#include "midi_app.h"
#include "sensor_data.h"
#include "crc.h"
#include "tinyusb/tusb.h"

#include <util/atomic.h>

#include "opt3001.h"
#include "stcc4.h"
#include "velocity_sensor.h"
#include "smAverage.h" //平均化ユーティリティ
#include "serial.h"

// <editor-fold defaultstate="collapsed" desc="通信仕様">

/*
 * MIDIのSysEx送受信の内部に独自仕様の通信を埋め込む。
 * 1byte: コマンド,  2byte以降: データ
 * H: Host, D: Deviceとする
 */
#define CMD_REPORT_DATA            0x01  // D->H: 現在の計測値（14 byte）を送信する
#define CMD_REQ_DATA               0x02  // D->H: 現在の計測値送信命令
#define CMD_START_MEAS             0x03  // H->D: 計測開始命令
#define CMD_STOP_MEAS              0x04  // H->D: 計測停止命令
#define CMD_COEF_A_DATA            0x05  // H<->D: 風速計の補正係数A（20 byte+CRC）を送信する。H->Dの場合にはDeviceに保存された係数が上書きされる。
#define CMD_REQ_COEF_A             0x06  // H->D: 風速計の補正係数A送信命令。Deviceは補正係数を1回送る。
#define CMD_COEF_B_DATA            0x07  // H<->D: 風速計の補正係数B（20 byte+CRC）を送信する。H->Dの場合にはDeviceに保存された係数が上書きされる。
#define CMD_REQ_COEF_B             0x08  // H->D: 風速計の補正係数B送信命令。Deviceは補正係数を1回送る。
#define CMD_ID_DATA                0x09  // H<-D: IDを送信する
#define CMD_REQ_ID                 0x10  // H->D: IDの送信命令
#define CMD_VERSION_DATA           0x11  // H<-D: Version (3 byte)を送信する
#define CMD_REQ_VERSION            0x12  // H->D: Version送信命令
#define CMD_CO2_CALIB_RESULT       0x13  // H<-D: CO2センサの補正結果（補正値（差分）: 2 byte）を送信する
#define CMD_REQ_CO2_CALIB          0x14  // H->D: CO2センサの補正命令（補正値: 2 byte）
#define CMD_REQ_CO2_FACTORY_RESET  0x15  // H->D: CO2センサの工場出荷時初期化命令
#define CMD_CO2_FACTORY_RESET_DONE 0x16  // H<-D: CO2センサの工場出荷時初期化成功通知

#define SYSEX_MANUFACTURER_ID      0x7D  // 教育・開発用ID

// </editor-fold>

// バージョン
#define VERSION_MAJOR (1)
#define VERSION_MINOR (0)
#define VERSION_REVISION (0)

// 計測時間間隔 [msec]
#define MEAS_CO2_SPAN (1000)
#define MEAS_ILL_SPAN (800)
#define MEAS_VEL_SPAN (200)

#define SYSEX_BUF_SIZE 64
static uint8_t rx_buf[SYSEX_BUF_SIZE];
static uint16_t rx_idx = 0;

// 時間経過[msec]（main.cから参照）
extern volatile uint32_t system_millis;

// 計測状態を管理するフラグ
bool MIDI_Measuring = false;

// 最終計測時間
static uint32_t last_meas_co2 = 0;
static uint32_t last_meas_vel = 0;
static uint32_t last_meas_ill = 0;

// 現在の計測値
static volatile SensorData_t current_data;

static SmAverage smaCO2; // 60秒平均を計算するインスタンス

// <editor-fold defaultstate="collapsed" desc="内部関数">

// 4bitニブル分割してパケット送信
static void send_nibbles(uint8_t val, uint8_t* buffer, uint8_t* buf_idx) {
    uint8_t nibbles[2] = { (val >> 4) & 0x0F, val & 0x0F };
    for (uint8_t i = 0; i < 2; i++) {
        buffer[(*buf_idx)++] = nibbles[i];
        if (*buf_idx == 3) {
            uint8_t packet[4] = { 0x04, buffer[0], buffer[1], buffer[2] };
            tud_midi_packet_write(packet);
            *buf_idx = 0;
        }
    }
}

// ニブルをバイトに復元する内部関数
static void decode_and_process_sysex(uint8_t* encoded_data, uint16_t len) {
    uint8_t decoded[SYSEX_BUF_SIZE / 2];
    uint16_t d_idx = 0;

    // 最初の2バイトは Manufacturer ID (7D) と Command ID なのでスキップせず解析
    uint8_t manufacturer_id = encoded_data[0];
    uint8_t command_id = encoded_data[1];

    if (manufacturer_id != SYSEX_MANUFACTURER_ID) return;

    // 3バイト目以降がニブル分割されたデータ本体
    for (uint16_t i = 2; i < len; i += 2) {
        decoded[d_idx++] = (encoded_data[i] << 4) | encoded_data[i+1];
    }

    // コマンド分岐**********************
    float coef_tmp[5];

    switch (command_id) {
        case CMD_REQ_DATA:
            MIDI_SendSysEx(CMD_REPORT_DATA, (uint8_t*)&current_data, sizeof(SensorData_t));
            break;
        
        case CMD_START_MEAS: //計測開始命令。以降、1sec毎に現在の計測値が送られ続ける
            MIDI_Measuring = true; 
            break;
            
        case CMD_STOP_MEAS: //計測停止命令
            MIDI_Measuring = false; 
            break;

        case CMD_COEF_A_DATA: // 係数A受信
            if (d_idx >= 21 && CRC_calc8(decoded, 20) == decoded[20]) 
                VELS_writeCoefficients((float*)decoded, true);
            break;

        case CMD_REQ_COEF_A: // 係数A要求
            if (VELS_readCoefficients(coef_tmp, true)) 
                MIDI_SendSysEx(CMD_COEF_A_DATA, (uint8_t*)coef_tmp, 20);
            break;

        case CMD_COEF_B_DATA: // 係数B受信
            if (d_idx >= 21 && CRC_calc8(decoded, 20) == decoded[20]) 
                VELS_writeCoefficients((float*)decoded, false);
            break;

        case CMD_REQ_COEF_B: // 係数B要求
            if (VELS_readCoefficients(coef_tmp, false)) 
                MIDI_SendSysEx(CMD_COEF_B_DATA, (uint8_t*)coef_tmp, 20);
            break;
            
        case CMD_REQ_ID: // シリアル番号送信要求
            {
                uint32_t hash = SERIAL_get_serial_hash();                
                // 4バイトのバイナリデータとしてパケット準備
                uint8_t hash_bytes[4];
                hash_bytes[0] = (uint8_t)(hash >> 24);
                hash_bytes[1] = (uint8_t)(hash >> 16);
                hash_bytes[2] = (uint8_t)(hash >> 8);
                hash_bytes[3] = (uint8_t)(hash);
                MIDI_SendSysEx(CMD_ID_DATA, hash_bytes, 4);
            }
            break;
            
        case CMD_REQ_VERSION: // Version送信要求
            {
                uint8_t version[3] = { VERSION_MAJOR, VERSION_MINOR, VERSION_REVISION };
                MIDI_SendSysEx(CMD_VERSION_DATA, version, 3);
            }
            break;
            
        case CMD_REQ_CO2_CALIB: // CO2校正要求
            {
                if (d_idx >= 3 && CRC_calc8(decoded, 2) == decoded[2]) {
                    uint16_t co2lvl = (decoded[0] << 8) | decoded[1];
                    uint16_t corct;
                    STCC4_performForcedRecalibration(co2lvl, &corct);
                    uint8_t corct_bytes[2] = { (uint8_t)(corct >> 8), (uint8_t)(corct & 0xFF) };
                    MIDI_SendSysEx(CMD_CO2_CALIB_RESULT, corct_bytes, 2);
                }                
            }
            break;
            
        case CMD_REQ_CO2_FACTORY_RESET: // CO2センサの工場出荷時初期化要求
            if(STCC4_performFactoryReset())
                MIDI_SendSysEx(CMD_CO2_FACTORY_RESET_DONE, NULL, 0);
            break;
    }
}

// </editor-fold>

// <editor-fold defaultstate="collapsed" desc="公開関数">

void MIDI_APP_Initialize(void)
{
    SMA_Init(&smaCO2); //CO2センサ平均化インスタンスの初期化
}

// 汎用SysEx送信関数
void MIDI_SendSysEx(uint8_t command_id, uint8_t* data, uint16_t len) {
    if (!tud_midi_mounted()) return;

    uint8_t buffer[3];
    uint8_t buf_idx = 0;
    uint8_t crc = CRC_calc8(data, (uint8_t)len);

    // Header: F0, 7D, CommandID
    uint8_t head[4] = { 0x04, 0xF0, 0x7D, command_id };
    tud_midi_packet_write(head);

    // Data + CRC
    for (uint16_t i = 0; i <= len; i++) {
        send_nibbles((i < len) ? data[i] : crc, buffer, &buf_idx);
    }

    // Footer: F7
    buffer[buf_idx++] = 0xF7;
    uint8_t cin = (buf_idx == 1) ? 0x05 : (buf_idx == 2) ? 0x06 : 0x07;
    uint8_t footer[4] = { cin, buffer[0], (buf_idx > 1 ? buffer[1] : 0), (buf_idx > 2 ? buffer[2] : 0) };
    tud_midi_packet_write(footer);
}

uint32_t get_system_millis(){
    uint32_t now;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        now = system_millis;
    }
    return now;
}

void MIDI_APP_Tasks(void) {
    // 受信処理
    if (tud_midi_available()) {
        uint8_t packet[4];
        while (tud_midi_packet_read(packet)) {
            uint8_t cin = packet[0] & 0x0F; // CINを取得

            // CINごとの応答
            switch (cin) {
                case 0x04: // SysEx 開始 または 継続
                    for (int i = 1; i <= 3; i++) {
                        if (packet[i] == 0xF0) continue; // 開始バイトは飛ばす
                        if (rx_idx < SYSEX_BUF_SIZE) rx_buf[rx_idx++] = packet[i];
                    }
                    break;

                case 0x05: // SysEx 終了 (1バイト)
                case 0x06: // SysEx 終了 (2バイト)
                case 0x07: // SysEx 終了 (3バイト)
                    {
                        uint8_t end_bytes = (cin == 0x05) ? 1 : (cin == 0x06) ? 2 : 3;
                        for (int i = 1; i <= end_bytes; i++) {
                            if (packet[i] == 0xF7) break; // 終了バイトに達したら終わり
                            if (rx_idx < SYSEX_BUF_SIZE) rx_buf[rx_idx++] = packet[i];
                        }
                        // 全データが揃ったので解析
                        decode_and_process_sysex(rx_buf, rx_idx);
                        rx_idx = 0; // バッファリセット
                    }
                    break;

                case 0x08: // ノートOff
                    break; // スルー
                    
                case 0x09: // ノートOn
                    break; // スルー
                    
                case 0x0B: // Control Change
                    break; // スルー

                default:
                    // その他のメッセージはスルー
                    break;
            }
        }
    }
    
    // 計測処理 (計測中のみ)
    if (MIDI_Measuring) {
        
        // 照度
        if (MEAS_ILL_SPAN <= get_system_millis() - last_meas_ill)
        {
            float ill_d;
            if(OPT3001_ReadALS(&ill_d))
            { 
                current_data.illuminance = (uint32_t)(10 * ill_d);
                current_data.status |= (1 << 0);
                
                last_meas_ill = get_system_millis();
            }
            else
            {
                current_data.status &= ~(1 << 0); // 計測失敗
                last_meas_ill += 10; // 10msec休んで再トライ
            }
        }
        
        // CO2, 温度, 湿度
        if (MEAS_CO2_SPAN <= get_system_millis() - last_meas_co2)
        {
            uint16_t co2_u = 0;
            float tmp_f = 0;
            float hmd_f = 0;
            if(STCC4_readMeasurement(&co2_u, &tmp_f, &hmd_f))
            {
                SMA_Add(&smaCO2, co2_u);
                uint16_t co2Ave = SMA_GetAverage(&smaCO2);
                current_data.co2_ppm = co2Ave;
                current_data.temperature = (int16_t)(100 * tmp_f);
                current_data.humidity = (uint16_t)(100 * hmd_f);
                current_data.status |= (1 << 1);
                
                last_meas_co2 = get_system_millis();
            }
            else
            {
                current_data.status &= ~(1 << 1); // 計測失敗
                last_meas_co2 += 50; // 50msec休んで再トライ
            }
        }
        
        // 風速
        if (MEAS_VEL_SPAN <= get_system_millis() - last_meas_vel)
        {
            uint16_t velocity;
            uint16_t voltage;
            if(VELS_readMeasurement(&velocity, &voltage))
            {
                current_data.velocity = velocity;
                current_data.voltage = voltage;
                current_data.status |= (1 << 2);
                
                last_meas_vel = get_system_millis();
            }
            else
            {
                current_data.status &= ~(1 << 2); // 計測失敗
                last_meas_vel += 10; // 10msec休んで再トライ
            }
        }        
    }
}

// </editor-fold>

#include "midi_app.h"
#include "sensor_data.h"
#include "crc.h"
#include "tinyusb/tusb.h"

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
 * 
 * コマンド一覧
 * 0x01: D->H: 現在の計測値（14 byte）を送信する
 * 0x02: H->D: 計測開始命令。以降、1sec毎に現在の計測値が送られ続ける
 * 0x03: H->D: 計測停止命令
 * 0x04: H<->D: 風速計の補正係数A（20 byte+CRC）を送信する。H->Dの場合にはDeviceに保存された係数が上書きされる。
 * 0x05: H->D: 風速計の補正係数A送信命令。Deviceは補正係数を1回送る。
 * 0x06: H<->D: 風速計の補正係数B（20 byte+CRC）を送信する。H->Dの場合にはDeviceに保存された係数が上書きされる。
 * 0x07: H->D: 風速計の補正係数B送信命令。Deviceは補正係数を1回送る。
 * 0x08: H<-D: IDを送信する
 * 0x09: H->D: IDの送信命令
 * 0x10: H<-D: Version (3 byte)を送信する
 * 0x11: H->D: Version送信命令
 * 0x12: H<-D: CO2センサの補正結果（補正値（差分）: 2 byte）を送信する
 * 0x13: H->D: CO2センサの補正命令（補正値: 2 byte）
 * 0x14: H->D: CO2センサの工場出荷時初期化命令
 * 0x15: H<-D: CO2センサの工場出荷時初期化成功通知
 */

// </editor-fold>

// バージョン
#define VERSION_MAJOR (1)
#define VERSION_MINOR (0)
#define VERSION_REVISION (0)

#define SYSEX_BUF_SIZE 64
static uint8_t rx_buf[SYSEX_BUF_SIZE];
static uint16_t rx_idx = 0;

// 計測状態を管理するフラグと時間管理
bool MIDI_Measuring = false;
static uint32_t last_send_ms = 0;
extern volatile uint32_t system_millis; // main.c から参照

static volatile SensorData_t current_data; //現在の計測値

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

    if (manufacturer_id != 0x7D) return;

    // 3バイト目以降がニブル分割されたデータ本体
    for (uint16_t i = 2; i < len; i += 2) {
        decoded[d_idx++] = (encoded_data[i] << 4) | encoded_data[i+1];
    }

    // コマンド分岐**********************
    float coef_tmp[5];

    switch (command_id) {
        case 0x02: //0x02: H->D: 計測開始命令。以降、1sec毎に現在の計測値が送られ続ける
            MIDI_Measuring = true; 
            last_send_ms = system_millis; // 開始直後に送信されるようにリセット
            break;
            
        case 0x03: //0x03: H->D: 計測停止命令
            MIDI_Measuring = false; 
            break;

        case 0x04: // 係数A受信
            if (d_idx >= 21 && CRC_calc8(decoded, 20) == decoded[20]) 
                VELS_writeCoefficients((float*)decoded, true);
            break;

        case 0x05: // 係数A要求
            if (VELS_readCoefficients(coef_tmp, true)) 
                MIDI_SendSysEx(0x04, (uint8_t*)coef_tmp, 20);
            break;

        case 0x06: // 係数B受信
            if (d_idx >= 21 && CRC_calc8(decoded, 20) == decoded[20]) 
                VELS_writeCoefficients((float*)decoded, false);
            break;

        case 0x07: // 係数B要求
            if (VELS_readCoefficients(coef_tmp, false)) 
                MIDI_SendSysEx(0x06, (uint8_t*)coef_tmp, 20);
            break;
            
        case 0x09: // シリアル番号送信要求
            {
                uint32_t hash = SERIAL_get_serial_hash();                
                // 4バイトのバイナリデータとしてパケット準備
                uint8_t hash_bytes[4];
                hash_bytes[0] = (uint8_t)(hash >> 24);
                hash_bytes[1] = (uint8_t)(hash >> 16);
                hash_bytes[2] = (uint8_t)(hash >> 8);
                hash_bytes[3] = (uint8_t)(hash);
                MIDI_SendSysEx(0x08, hash_bytes, 4);
            }
            break;
            
        case 0x11: // Version送信要求
            {
                uint8_t version[3] = { VERSION_MAJOR, VERSION_MINOR, VERSION_REVISION };
                MIDI_SendSysEx(0x10, version, 3);
            }
            break;
            
        case 0x13: // CO2校正要求
            {
                if (d_idx >= 3 && CRC_calc8(decoded, 2) == decoded[2]) {
                    uint16_t co2lvl = (decoded[0] << 8) | decoded[1];
                    uint16_t corct;
                    STCC4_performForcedRecalibration(co2lvl, &corct);
                    uint8_t corct_bytes[2] = { (uint8_t)(corct >> 8), (uint8_t)(corct & 0xFF) };
                    MIDI_SendSysEx(0x12, corct_bytes, 2);
                }                
            }
            break;
            
        case 0x14: // CO2センサの工場出荷時初期化要求
            if(STCC4_performFactoryReset())
                MIDI_SendSysEx(0x15, NULL, 0);
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
    
    // 定期送信処理 (計測中のみ)
    if (MIDI_Measuring) {
        if (system_millis - last_send_ms >= 1000) {
            last_send_ms = system_millis;
            
            // 計測成功真偽フラグ
            current_data.status = 0;
            
            // 照度
            float ill_d;
            if(OPT3001_ReadALS(&ill_d))
            { 
                current_data.illuminance = (uint32_t)(10 * ill_d);
                current_data.status |= (1 << 0);
            }
            
            // CO2, 温湿度
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
            }
            
            uint16_t velocity;
            uint16_t voltage;
            if(VELS_readMeasurement(&velocity, &voltage))
            {
                current_data.velocity = velocity;
                current_data.voltage = voltage;
                current_data.status |= (1 << 2);
            }
            
            // コマンド0x01で送信
            MIDI_SendSysEx(0x01, (uint8_t*)&current_data, sizeof(SensorData_t));
        }
    }
}

// </editor-fold>

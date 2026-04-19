/* 
 * File:   midi_app.h
 * Author: e.togashi
 *
 * Created on 2026/03/09, 14:45
 */

#ifndef MIDI_APP_H
#define	MIDI_APP_H

#ifdef	__cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>
 
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
#define CMD_REQ_CONDITIONING       0x17  // H->D: CO2初期調整命令
#define CMD_CONDITIONING_START     0x18  // D->H: CO2初期調整開始通知
#define CMD_CONDITIONING_DONE      0x19  // D->H: CO2初期調整完了通知

#define SYSEX_MANUFACTURER_ID      0x7D  // 教育・開発用ID

// </editor-fold>
    
// 初期化
void MIDI_APP_Initialize(void);

// タスク
void MIDI_APP_Tasks(void);

// 汎用SysEx送信
void MIDI_SendSysEx(uint8_t command_id, uint8_t* data, uint16_t len);

#ifdef	__cplusplus
}
#endif

#endif	/* MIDI_APP_H */


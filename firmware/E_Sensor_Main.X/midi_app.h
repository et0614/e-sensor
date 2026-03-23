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


/* 
 * File:   eeprom_manager.h
 * Author: e.togashi
 *
 * Created on 2026/03/23, 10:05
 */

#ifndef EEPROM_MANAGER_H
#define	EEPROM_MANAGER_H

#ifdef	__cplusplus
extern "C" {
#endif

extern uint8_t EM_Sensing_Enabled;

extern uint8_t EM_Is_First_Run;
    
//EEPROMを読み込む
void EM_loadEEPROM();

//設定を保存する
void EM_updateEEPROM();

//EEPROMを初期化する
void EM_resetEEPROM();


#ifdef	__cplusplus
}
#endif

#endif	/* EEPROM_MANAGER_H */


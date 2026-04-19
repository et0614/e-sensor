#include "mcc_generated_files/nvm/nvm.h"
#include "eeprom_manager.h"

// AVR64DU32 EEPROM開始アドレス (定数として定義)
#define EEPROM_BASE_ADDR  0x1400

// EEPROM Busy 待ちの上限（反復回数）。EEPROM 1 バイト書き込みは実デバイス仕様で
// 11 ms 程度。F_CPU = 24 MHz、空ループ 1 回あたり数 cycle なので十分な余裕。
#define EEPROM_WAIT_MAX_ITER  200000UL

// EEPROM の完了待ち。タイムアウトしても返すだけで、呼出側には副作用を強要しない。
static bool eeprom_wait_ready(void)
{
    uint32_t iter = 0;
    while (EEPROM_IsBusy())
    {
        if (++iter > EEPROM_WAIT_MAX_ITER) return false;
    }
    return true;
}

// EEPROM全体のマップを定義（型定義のみ）
typedef struct {
    uint8_t init_flag;      // 0x0000
    uint8_t sensing_enabled;
    uint8_t is_first_run;
} EepromMap;

// 自動的にアドレス数値に変換
#define ADDR_INIT_FLAG        (EEPROM_BASE_ADDR +offsetof(EepromMap, init_flag))
#define ADDR_SENSING_ENABLED  (EEPROM_BASE_ADDR +offsetof(EepromMap, sensing_enabled))
#define ADDR_IS_FIRST_RUN     (EEPROM_BASE_ADDR +offsetof(EepromMap, is_first_run))

uint8_t EM_Sensing_Enabled = true;
uint8_t EM_Is_First_Run = true;
static uint8_t last_sensing_enabled;
static uint8_t last_run_state;

//EEPROMを読み込む
void EM_loadEEPROM()
{
    //ビジーチェック
    eeprom_wait_ready();

    //初期化未了の場合は初期化
    if (EEPROM_Read(ADDR_INIT_FLAG) != 'T') EM_resetEEPROM();

    //計測有効無効フラグ
    eeprom_wait_ready();
    EM_Sensing_Enabled = EEPROM_Read(ADDR_SENSING_ENABLED);
    last_sensing_enabled = EM_Sensing_Enabled;

    //初回起動フラグ
    eeprom_wait_ready();
    EM_Is_First_Run = EEPROM_Read(ADDR_IS_FIRST_RUN);
    last_run_state = EM_Is_First_Run;
}

//設定を保存する
void EM_updateEEPROM()
{
    if(last_sensing_enabled != EM_Sensing_Enabled)
    {
        last_sensing_enabled = EM_Sensing_Enabled;
        if (eeprom_wait_ready())
            EEPROM_Write(ADDR_SENSING_ENABLED, EM_Sensing_Enabled);
    }

    if(last_run_state != EM_Is_First_Run)
    {
        last_run_state = EM_Is_First_Run;
        if (eeprom_wait_ready())
            EEPROM_Write(ADDR_IS_FIRST_RUN, EM_Is_First_Run);
    }
}

//EEPROMを初期化する
void EM_resetEEPROM()
{
    //センシング有効・無効
    if (eeprom_wait_ready())
        EEPROM_Write(ADDR_SENSING_ENABLED, 1); //有効

    //初回起動フラグ
    if (eeprom_wait_ready())
        EEPROM_Write(ADDR_IS_FIRST_RUN, 1); //有効

    if (eeprom_wait_ready())
        EEPROM_Write(ADDR_INIT_FLAG, 'T'); //初期化完了
}

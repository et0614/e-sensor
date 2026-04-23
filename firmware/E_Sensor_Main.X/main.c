#include "mcc_generated_files/system/system.h"
#include "mcc_generated_files/system/ccp.h"
#include "mcc_generated_files/timer/tca0.h"
#include "mcc_generated_files/system/pins.h"

#include <avr/interrupt.h>
#include <avr/wdt.h>

#include "tinyusb/tusb.h"
#include "midi_app.h"
#include "opt3001.h"
#include "velocity_sensor.h"
#include "stcc4.h"
#include "eeprom_manager.h"

#include <util/atomic.h>

volatile uint32_t system_millis = 0;
volatile uint32_t sec_timer = 0;
// CO2 初期調整用のタイマ。休眠状態は -1、調整要求受信時に 0 にセットされ、
// ISR が 0 以上の場合のみ 1msec 刻みでインクリメントする。起動直後に
// CMD_CONDITIONING_DONE が誤発火しないよう -1 で初期化する。
volatile int32_t co2_pfm_timer = -1;

volatile bool conditioning_requested = false;

// 1msecごとのコールバック関数
void msecHandler(void)
{
    // ISR内なので他割り込みによる競合は無い。単純インクリメントでよい。
    system_millis++;
    sec_timer++;
    if(0 <= co2_pfm_timer) co2_pfm_timer++;
}

// 32bit変数をアトミックに読むヘルパ
static inline uint32_t atomic_load_u32(volatile uint32_t *p) {
    uint32_t v;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { v = *p; }
    return v;
}
static inline int32_t atomic_load_i32(volatile int32_t *p) {
    int32_t v;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { v = *p; }
    return v;
}
static inline void atomic_store_u32(volatile uint32_t *p, uint32_t v) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { *p = v; }
}
static inline void atomic_store_i32(volatile int32_t *p, int32_t v) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { *p = v; }
}

// TinyUSBが参照する時間取得関数をオーバーライド
uint32_t tusb_time_millis_api(void) {
    uint32_t now;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
        now = system_millis;
    }
    return now;
}

void setup_usb(void) {
    // 1. 周波数を 24MHz に設定 (USB動作のベース)
    // 2. AUTOTUNE を SOF (USB Start of Frame) に同期する設定 (0x02) にする
    ccp_write_io((void *)&(CLKCTRL.OSCHFCTRLA), CLKCTRL_FRQSEL_24M_gc | CLKCTRL_AUTOTUNE_SOF_gc);
    
    SYSCFG.VUSBCTRL = SYSCFG_USBVREG_bm;
    
     //Tiny USB初期化
     tusb_init();
     
     // PCに対してUSB接続を通知
    USB0.CTRLB |= USB_ATTACH_bm;
}

int main(void)
{
    SYSTEM_Initialize();

    // ウォッチドッグ有効化（~4秒）。以降、main ループで wdt_reset() を必ず呼ぶこと。
    // I2C / EEPROM 等のポーリング待ちがハングしても、この時間内に wdt_reset() に到達
    // できなければ MCU が自動でリセットされる。
    ccp_write_io((void *)&WDT.CTRLA, WDT_PERIOD_4KCLK_gc);

    // EEPROM読み込み
    EM_loadEEPROM();
    
    // センサ初期化
    STCC4_initialize();
    OPT3001_Initialize();
    VELS_start();
    
    // STCC4は初回起動時のみ工場出荷状態に初期化
    if(EM_Is_First_Run)
    {
        if(STCC4_performFactoryReset())
        {
            //成功したらフラグ解除。失敗の場合は次回起動時に持ち越す
            EM_Is_First_Run = false;
            EM_updateEEPROM();
        }
    }
    STCC4_exitSleep();
    STCC4_startContinuousMeasurement(); //CO2連続計測開始
    
    MIDI_APP_Initialize();
    
    // イベントハンドラ登録
    TCA0_OverflowCallbackRegister(msecHandler); // 1msecタイマ
    
    // USB通信用意
    setup_usb();

    // 割り込み開始
    sei();
    
    B_LED_SetLow();
    
    while(1)
    {
        // ウォッチドッグキック
        wdt_reset();

        tud_task();         // USBスタック
        MIDI_APP_Tasks();   // MIDIアプリ処理

        // CO2センサ初期調整関連
        if(conditioning_requested && STCC4_performConditioning())
        {
            conditioning_requested = false;
            MIDI_SendSysEx(CMD_CONDITIONING_START, NULL, 0);
            atomic_store_i32(&co2_pfm_timer, 0);
        }
        // 初期調整（約22sec）が終わったらCO2センサの連続計測開始
        if(23000 < atomic_load_i32(&co2_pfm_timer))
        {
            if(STCC4_startContinuousMeasurement())
            {
                MIDI_SendSysEx(CMD_CONDITIONING_DONE, NULL, 0);
                atomic_store_i32(&co2_pfm_timer, -1);
            }
            else
            {
                // 失敗時は I2C を叩き続けないよう 1sec バックオフして再試行
                atomic_store_i32(&co2_pfm_timer, 22000);
            }
        }

        // 1secタイマ
        if(1000 < atomic_load_u32(&sec_timer))
        {
            atomic_store_u32(&sec_timer, 0);

            if(EM_Sensing_Enabled) B_LED_Toggle();
            else B_LED_SetLow();
        }
    }
}
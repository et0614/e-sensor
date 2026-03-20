/* 
 * File:   sensor_data.h
 * Author: etoga
 *
 * Created on 2026/03/09, 14:43
 */

#ifndef SENSOR_DATA_H
#define	SENSOR_DATA_H

#ifdef	__cplusplus
extern "C" {
#endif

#include <stdint.h>

// データ構造体の定義 (14 bytes)
typedef struct __attribute__((packed)) {
    uint32_t illuminance;   // 照度 (単位: Lux * 10)
    int16_t  temperature;   // 乾球温度 (単位: ℃ * 100)
    uint16_t humidity;      // 相対湿度 (単位: % * 100)
    uint16_t velocity;      // 風速 (単位: mm/s * 10000)
    uint16_t voltage;       // 風速推定のための電圧 (単位: mV)
    uint16_t co2_ppm;       // CO2濃度 (単位: ppm)
    uint8_t  status;        // Bit0: 照度, Bit1: 温湿度CO2, Bit2: 風速(Vel/Vol)
} SensorData_t;

#ifdef	__cplusplus
}
#endif

#endif	/* SENSOR_DATA_H */


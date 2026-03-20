/* 
 * File:   velocity_sensor.h
 * Author: etoga
 *
 * Created on 2026/03/09, 16:11
 */

#ifndef VELOCITY_SENSOR_H
#define	VELOCITY_SENSOR_H

#ifdef	__cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>
    
/**
* @brief 風速センサを起動する
* @return 起動成功の真偽
*/
bool VELS_start();

/**
* @brief 風速センサを停止する
* @return 停止成功の真偽
*/
bool VELS_stop();

/**
* @brief フィルタ番号を読み込む
* @return 通信成功の真偽
*/
bool VELS_readFilter(uint8_t *filter);

/**
* @brief フィルタ番号を書き込む
* @return 通信成功の真偽
*/
bool VELS_writeFilter(uint8_t filter);

/**
* @brief 補正係数を読み込む
* @return 通信成功の真偽
*/
bool VELS_readCoefficients(float * coeffs, bool isA);

/**
* @brief 補正係数を書き込む
* @return 通信成功の真偽
*/
bool VELS_writeCoefficients(float * coeffs, bool isA);

/**
* @fn
* 計測結果を読む
* @return 成功でtrue、失敗でfalse
*/
bool VELS_readMeasurement(uint16_t * velocity, uint16_t * voltage);


#ifdef	__cplusplus
}
#endif

#endif	/* VELOCITY_SENSOR_H */


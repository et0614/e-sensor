#include <string.h>

#include "velocity_sensor.h"
#include "i2c_master.h"
#include "crc.h"

//アドレス
#define VEL_ADD (0x10)

typedef struct __attribute__((packed)){
    // [Read Only Area] -------------------------
    // 電圧[mV] (0-2000)
    uint8_t voltage_high;
    uint8_t voltage_low;
    uint8_t voltage_crc;
    
    // 風速[mm/s] (0-10000)
    uint8_t velocity_high;
    uint8_t velocity_low;
    uint8_t velocity_crc;
    
    // マイコン温度[10C]
    uint8_t mcu_temp_high;
    uint8_t mcu_temp_low;
    uint8_t mcu_temp_crc;
    
    //予熱中か否か
    uint8_t status;
    
    //バージョン
    uint8_t version;
    
    //製造ID
    uint8_t id[16];
    uint8_t id_crc;
    uint8_t id_hash[4];
    uint8_t id_hash_crc;
    
    // [Read/Write Area] ------------------------
    uint8_t enable;
    uint8_t filter_n;
    uint8_t updated;
    
    //電圧→風速換算用係数
    float coefficientA[5];
    uint8_t crc_coefA;
    float coefficientB[5];
    uint8_t crc_coefB;
    
    //I2Cアドレス変更用のロック
    uint8_t i2c_addr_unlock; 
    // I2Cアドレス
    uint8_t i2c_address;

} VelocitySensorData_t;

// アドレス定義の自動生成
typedef enum {
    ADDR_VOLTAGE  = offsetof(VelocitySensorData_t, voltage_high),
    ADDR_VELOCITY = offsetof(VelocitySensorData_t, velocity_high),
    ADDR_MCU_TEMP = offsetof(VelocitySensorData_t, mcu_temp_high),
    ADDR_STATUS   = offsetof(VelocitySensorData_t, status),
    ADDR_ENABLE   = offsetof(VelocitySensorData_t, enable),
    ADDR_FILTER   = offsetof(VelocitySensorData_t, filter_n),
    ADDR_COEF_A   = offsetof(VelocitySensorData_t, coefficientA),
    ADDR_COEF_B   = offsetof(VelocitySensorData_t, coefficientB),
    ADDR_I2C_ADDR = offsetof(VelocitySensorData_t, i2c_address),
} VelocitySensorAddr_t;

bool VELS_start()
{
    uint8_t tx_data[2];
    tx_data[0] = ADDR_ENABLE;
    tx_data[1] = 1;
	return I2C_Write(VEL_ADD, tx_data, 2);
}

bool VELS_stop()
{
    uint8_t tx_data[2];
    tx_data[0] = ADDR_ENABLE;
    tx_data[1] = 0;
	return I2C_Write(VEL_ADD, tx_data, 2);
}

bool VELS_readFilter(uint8_t *filter)
{
    const uint8_t cmd = ADDR_FILTER;
    uint8_t buffer[1];
    if (!I2C_WriteRead(VEL_ADD, &cmd, 1, buffer, 1)) return false;
    
    *filter = buffer[0];
    return true;
}

bool VELS_writeFilter(uint8_t filter)
{
    uint8_t tx_data[2];
    tx_data[0] = ADDR_FILTER;
    tx_data[1] = filter;
	return I2C_Write(VEL_ADD, tx_data, 2);
}

bool VELS_readCoefficients(float * coeffs, bool isA)
{
    const uint8_t addr = isA ? ADDR_COEF_A : ADDR_COEF_B;
    uint8_t buffer[21]; // 20 bytes (float[5]) + 1 byte (CRC)

    // 21バイト分を読み出す
    if (!I2C_WriteRead(VEL_ADD, &addr, 1, buffer, 21)) return false;

    // データ部分(20B)のCRCをチェック
    if (CRC_calc8(buffer, 20) != buffer[20]) return false;

    // バッファからfloat配列へ一括コピー
    memcpy(coeffs, buffer, 20);
    
    return true;
}

bool VELS_writeCoefficients(float * coeffs, bool isA)
{
    const uint8_t addr = isA ? ADDR_COEF_A : ADDR_COEF_B;
    uint8_t buffer[22]; // 1 byte (addr) + 20 bytes (data) + 1 byte (CRC)

    // 送信バッファの組み立て
    buffer[0] = addr;
    memcpy(&buffer[1], coeffs, 20);
    
    // データ部分(21バイト目)にCRCを付与
    buffer[21] = CRC_calc8(&buffer[1], 20);

    // I2Cで一括送信 [Address, Data0...Data19, CRC]
    return I2C_Write(VEL_ADD, buffer, 22);
}

bool VELS_readMeasurement(uint16_t * velocity, uint16_t * voltage)
{
    const uint8_t cmd = ADDR_VOLTAGE;
    uint8_t buffer[6]; // 電圧(H, L, CRC) + 風速(H, L, CRC) の計6バイト

    // 1. 電圧のアドレスから6バイト分を一括で読み出す
    if (!I2C_WriteRead(VEL_ADD, &cmd, 1, buffer, 6)) return false;

    // 2. 電圧データの整合性確認 (電圧レジスタは buffer[0:2])
    if (CRC_calc8(&buffer[0], 2) != buffer[2]) return false;
    *voltage = (uint16_t)((buffer[0] << 8) | buffer[1]);

    // 3. 風速データの整合性確認 (風速レジスタは buffer[3:5])
    if (CRC_calc8(&buffer[3], 2) != buffer[5]) return false;
    *velocity = (uint16_t)((buffer[3] << 8) | buffer[4]);
    
    return true;
}

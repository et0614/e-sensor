/* 
 * File:   serial.h
 * Author: etoga
 *
 * Created on 2026/03/19, 11:05
 */

#ifndef SERIAL_H
#define	SERIAL_H

#ifdef	__cplusplus
extern "C" {
#endif

#include <avr/io.h>
#include <string.h>

// FNV-1aでハッシュを計算する
static uint32_t SERIAL_fnv1a_32(const uint8_t* data, size_t len) {
    uint32_t hash = 2166136261U; // Offset basis
    for (size_t i = 0; i < len; i++) {
        hash ^= data[i];
        hash *= 16777619U; // FNV prime
    }
    return hash;
}

static uint32_t SERIAL_get_serial_hash()
{
    // AVR64DU32のシリアル番号は0x1090から始まる16バイト
    const uint8_t *sigrow_ptr = (const uint8_t*) &SIGROW_SERNUM0;
    return  SERIAL_fnv1a_32(sigrow_ptr, 16);
}

static void SERIAL_get_serial_string(char* buf) {
    const char hex_map[] = "0123456789ABCDEF";
    uint32_t hash_id = SERIAL_get_serial_hash();

    // 32ビット値を8桁の16進数文字列に変換 (Big Endian順)
    for (int i = 7; i >= 0; i--) {
        buf[i] = hex_map[hash_id & 0x0F];
        hash_id >>= 4;
    }
    buf[8] = '\0';
}

#ifdef	__cplusplus
}
#endif

#endif	/* SERIAL_H */


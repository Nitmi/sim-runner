#![no_main]
#![no_std]

use core::arch::{asm, global_asm};
use core::panic::PanicInfo;
use core::ptr::{read_volatile, write_volatile};

global_asm!(
    r#"
    .syntax unified
    .cpu cortex-m4
    .thumb
    .section .vector_table,"a",%progbits
    .align 2
    .word 0x20040000
    .word Reset
    .rept 22
    .word DefaultHandler
    .endr
    .word Timer0
    .rept 39
    .word DefaultHandler
    .endr
    .section .text.Reset,"ax",%progbits
    .global Reset
    .type Reset,%function
    .thumb_func
Reset:
    cpsid i
    bl rust_main
    b .
    .global DefaultHandler
    .type DefaultHandler,%function
    .thumb_func
DefaultHandler:
    b .
"#
);

const UART: usize = 0x4000_2000;
const TWI: usize = 0x4000_3000;
const TIMER: usize = 0x4000_8000;
static mut SEQUENCE: u32 = 0;
static mut FIRST_RAW: u8 = 0;

unsafe fn write(base: usize, offset: usize, value: u32) {
    unsafe { write_volatile((base + offset) as *mut u32, value) };
}
unsafe fn read(base: usize, offset: usize) -> u32 {
    unsafe { read_volatile((base + offset) as *const u32) }
}
unsafe fn send(bytes: &[u8]) {
    for &byte in bytes {
        unsafe {
            write(UART, 0x11c, 0);
            write(UART, 0x51c, u32::from(byte));
            while read(UART, 0x11c) == 0 {
                asm!("nop", options(nomem, nostack, preserves_flags));
            }
        }
    }
}
fn hex(line: &mut [u8], offset: usize, value: u32, digits: usize) {
    for index in 0..digits {
        line[offset + index] =
            b"0123456789ABCDEF"[((value >> ((digits - 1 - index) * 4)) & 15) as usize];
    }
}
unsafe fn wait_event(offset: usize) -> bool {
    for _ in 0..10000 {
        if unsafe { read(TWI, offset) } != 0 {
            return true;
        }
    }
    false
}
unsafe fn temperature() -> (u8, u32) {
    unsafe {
        write(TWI, 0x11c, 0); // TXDSENT
        write(TWI, 0x108, 0); // RXDREADY
        write(TWI, 0x104, 0); // STOPPED
        write(TWI, 0x008, 1); // STARTTX, then select TMP103 register 0
        write(TWI, 0x51c, 0);
        if !wait_event(0x11c) {
            return (0, 1);
        }
        // Renode 1.17 flushes the register pointer at STARTRX. STOP would clear it.
        write(TWI, 0x000, 1);
        if !wait_event(0x108) {
            return (0, 2);
        }
        let raw = read(TWI, 0x518) as u8;
        write(TWI, 0x014, 1);
        if !wait_event(0x104) {
            return (raw, 3);
        }
        (raw, 0)
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn Timer0() {
    unsafe {
        write(TIMER, 0x140, 0);
        write(TIMER, 0x044, 1);
        let captured = read(TIMER, 0x544);
        let vector = read(0xe000_ed00, 4) & 0x1ff;
        let sequence = read_volatile(&raw const SEQUENCE) + 1;
        write_volatile(&raw mut SEQUENCE, sequence);
        let (mut raw, error) = temperature();
        if sequence == 1 {
            write_volatile(&raw mut FIRST_RAW, raw);
        }
        if cfg!(sim_stale_temperature) {
            raw = read_volatile(&raw const FIRST_RAW);
        }
        let celsius = if cfg!(sim_unsigned_temperature) {
            i32::from(raw)
        } else {
            i32::from(raw as i8)
        };
        let mut line =
            *b"SIM_NRF_SENSOR_SAMPLE 00000000 R=00 C=00000000 T=00000000 V=00000000 E=00000000\r\n";
        hex(&mut line, 22, sequence, 8);
        hex(&mut line, 33, u32::from(raw), 2);
        hex(&mut line, 38, celsius as u32, 8);
        hex(&mut line, 49, captured, 8);
        hex(&mut line, 60, vector, 8);
        hex(&mut line, 71, error, 8);
        send(&line);
        if sequence < 16 {
            write(TIMER, 0x540, (sequence + 1) * 1250);
        } else {
            write(TIMER, 0x004, 1);
            write(TIMER, 0x308, 1 << 16);
        }
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        write(UART, 0x500, 4);
        write(UART, 0x524, 0x01d7_e000);
        write(UART, 0x008, 1);
        write(TWI, 0x500, 5);
        write(TWI, 0x588, 0x48);
        // PSEL and FREQUENCY are not implemented in Renode 1.17's NRF52840_I2C.
        send(b"SIM_NRF_SENSOR_READY v1\r\n");
        send(concat!("SIM_NRF_SENSOR_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(TIMER, 0x004, 1);
        write(TIMER, 0x00c, 1);
        write(TIMER, 0x504, 0);
        write(TIMER, 0x510, 6);
        write(TIMER, 0x540, 1250);
        write(TIMER, 0x304, 1 << 16);
        write_volatile(0xe000_e100 as *mut u32, 1 << 8);
        write(TIMER, 0x000, 1);
        asm!("cpsie i", options(nomem, nostack, preserves_flags));
    }
    loop {
        unsafe {
            asm!("wfi", options(nomem, nostack));
        }
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop {
        unsafe {
            asm!("nop", options(nomem, nostack, preserves_flags));
        }
    }
}

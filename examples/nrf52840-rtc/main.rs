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
    .rept 25
    .word DefaultHandler
    .endr
    .word RTC0
    .rept 36
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
const RTC: usize = 0x4000_b000;
static mut SEQUENCE: u32 = 0;

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
fn hex(line: &mut [u8], offset: usize, value: u32) {
    for index in 0..8 {
        line[offset + index] = b"0123456789ABCDEF"[((value >> ((7 - index) * 4)) & 15) as usize];
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn RTC0() {
    unsafe {
        let counter = read(RTC, 0x504);
        let event = read(RTC, 0x140);
        if !cfg!(sim_missing_clear) {
            write(RTC, 0x140, 0);
        }
        let after = read(RTC, 0x140);
        let sequence = read_volatile(&raw const SEQUENCE) + 1;
        write_volatile(&raw mut SEQUENCE, sequence);
        let next = if cfg!(sim_stale_compare) { 16 } else { (sequence + 1) * 16 };
        write(RTC, 0x540, next);
        let prescaler = read(RTC, 0x508);
        let vector = read(0xe000_ed00, 4) & 0x1ff;
        let mut line = *b"SIM_NRF_RTC_SAMPLE 00000000 C=00000000 E=00000000 A=00000000 N=00000000 P=00000000 V=00000000\r\n";
        hex(&mut line, 19, sequence);
        hex(&mut line, 30, counter);
        hex(&mut line, 41, event);
        hex(&mut line, 52, after);
        hex(&mut line, 63, next);
        hex(&mut line, 74, prescaler);
        hex(&mut line, 85, vector);
        send(&line);
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        write_volatile(&raw mut SEQUENCE, 0);
        write(UART, 0x500, 4);
        write(UART, 0x524, 0x01d7_e000);
        write(UART, 0x008, 1);
        send(b"SIM_NRF_RTC_READY v1\r\n");
        send(concat!("SIM_NRF_RTC_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(RTC, 0x008, 1);
        write(RTC, 0x508, if cfg!(sim_wrong_prescaler) { 15 } else { 31 });
        write(RTC, 0x540, 16);
        write(RTC, 0x304, 1 << 16);
        write_volatile(0xe000_e100 as *mut u32, 1 << 11);
        if !cfg!(sim_never_started) {
            write(RTC, 0, 1);
        }
        asm!("cpsie i", options(nomem, nostack, preserves_flags));
    }
    loop {
        unsafe { asm!("wfi", options(nomem, nostack)); }
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop {
        unsafe { asm!("nop", options(nomem, nostack, preserves_flags)); }
    }
}

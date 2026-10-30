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
const TIMER: usize = 0x4000_8000;
const WDT: usize = 0x4001_0000;
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
pub extern "C" fn Timer0() {
    unsafe {
        write(TIMER, 0x140, 0);
        write(TIMER, 0x044, 1);
        let captured = read(TIMER, 0x544);
        let vector = read(0xe000_ed00, 4) & 0x1ff;
        let sequence = read_volatile(&raw const SEQUENCE) + 1;
        write_volatile(&raw mut SEQUENCE, sequence);
        let magic = if cfg!(sim_wrong_reload_magic) { 0 } else { 0x6e52_4635 };
        write(WDT, 0x600, magic);
        let partial = read(WDT, 0x404);
        if !cfg!(sim_missing_second_channel) {
            write(WDT, 0x604, magic);
        }
        let complete = read(WDT, 0x404);
        let running = read(WDT, 0x400);
        let mut line = *b"SIM_NRF_WDT_SAMPLE 00000000 A=00000000 B=00000000 S=00000000 T=00000000 V=00000000\r\n";
        hex(&mut line, 19, sequence);
        hex(&mut line, 30, partial);
        hex(&mut line, 41, complete);
        hex(&mut line, 52, running);
        hex(&mut line, 63, captured);
        hex(&mut line, 74, vector);
        send(&line);
        write(TIMER, 0x540, (sequence + 1) * 1250);
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        // Reset entry does not use a runtime/BSS initializer. Initialize every boot.
        write_volatile(&raw mut SEQUENCE, 0);
        write(UART, 0x500, 4);
        write(UART, 0x524, 0x01d7_e000);
        write(UART, 0x008, 1);
        send(b"SIM_NRF_WDT_READY v1\r\n");
        send(concat!("SIM_NRF_WDT_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(WDT, 0x504, 1023); // (CRV+1)/32768 = 31.25 ms in this model
        write(WDT, 0x508, 3); // both RR0 and RR1 must be reloaded
        if !cfg!(sim_never_started) {
            write(WDT, 0, 1);
        }
        // CONFIG is deliberately untouched: Renode 1.17 implements it as a tag.
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
        unsafe { asm!("wfi", options(nomem, nostack)); }
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop {
        unsafe { asm!("nop", options(nomem, nostack, preserves_flags)); }
    }
}

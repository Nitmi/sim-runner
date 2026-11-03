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
    .rept 34
    .word DefaultHandler
    .endr
    .word EGU0
    .rept 27
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
const EGU: usize = 0x4001_4000;
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
pub extern "C" fn EGU0() {
    unsafe {
        // A masked pending channel must remain latched, even while siblings interrupt.
        let mask = read(EGU, 0x300);
        for channel in [0_usize, 3, 15] {
            let offset = 0x100 + channel * 4;
            let event = read(EGU, offset);
            if event == 0 || mask & (1 << channel) == 0 {
                continue;
            }
            if !cfg!(sim_missing_clear) {
                write(EGU, offset, 0);
            }
            let after = read(EGU, offset);
            let sequence = read_volatile(&raw const SEQUENCE) + 1;
            write_volatile(&raw mut SEQUENCE, sequence);
            let vector = read(0xe000_ed00, 4) & 0x1ff;
            let mut line = *b"SIM_NRF_EGU_SAMPLE 00000000 C=00000000 E=00000000 A=00000000 M=00000000 V=00000000\r\n";
            hex(&mut line, 19, sequence);
            hex(&mut line, 30, if cfg!(sim_wrong_channel) { 1 } else { channel as u32 });
            hex(&mut line, 41, event);
            hex(&mut line, 52, after);
            hex(&mut line, 63, mask);
            hex(&mut line, 74, vector);
            send(&line);
            // Bound the missing-clear negative fixture to one ISR log per task;
            // preserve the UART A=1 failure and leave the event uncleared.
            if cfg!(sim_missing_clear) {
                write(EGU, 0x308, 1 << channel);
            }
        }
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        write_volatile(&raw mut SEQUENCE, 0);
        write(UART, 0x500, 4);
        write(UART, 0x524, 0x01d7_e000);
        write(UART, 0x008, 1);
        send(b"SIM_NRF_EGU_READY v1\r\n");
        send(concat!("SIM_NRF_EGU_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(EGU, 0x304, if cfg!(sim_wrong_mask) { 0x8001 } else { 0x8009 });
        if !cfg!(sim_irq_disabled) {
            write_volatile(0xe000_e100 as *mut u32, 1 << 20);
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

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
    .rept 62
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

unsafe fn write(offset: usize, value: u32) {
    unsafe { write_volatile((UART + offset) as *mut u32, value) };
}

unsafe fn send(bytes: &[u8]) {
    for &byte in bytes {
        unsafe {
            write(0x11c, 0);
            write(0x51c, u32::from(byte));
            while read_volatile((UART + 0x11c) as *const u32) == 0 {
                asm!("nop", options(nomem, nostack, preserves_flags));
            }
        }
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    #[cfg(sim_unmapped_write)]
    unsafe {
        // Negative simulation fixture: address intentionally absent from our model.
        write_volatile(0x4000_0000 as *mut u32, 1);
    }
    unsafe {
        write(0x500, 0);
        write(0x524, 0x01d7_e000);
        write(0x56c, 0);
        write(0x500, 4);
        write(0x008, 1);
        send(b"SIM_NRF52840_READY v1\r\n");
        send(concat!("SIM_NRF52840_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
    }
    let mut sequence: u32 = 0;
    loop {
        for _ in 0..500_000 {
            unsafe { asm!("nop", options(nomem, nostack, preserves_flags)) };
        }
        sequence += 1;
        let mut line = *b"SIM_NRF52840_HEARTBEAT 00000000\r\n";
        for index in 0..8 {
            line[23 + index] = b"0123456789ABCDEF"[((sequence >> ((7 - index) * 4)) & 15) as usize];
        }
        unsafe { send(&line) };
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop {
        unsafe { asm!("nop", options(nomem, nostack, preserves_flags)) };
    }
}

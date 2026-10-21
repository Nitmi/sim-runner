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
const GPIO: usize = 0x5000_0500;
const TIMER: usize = 0x4000_8000;
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
        let input = (read(GPIO, 0x10) >> 11) & 1;
        let output = if cfg!(sim_invert_output) {
            input ^ 1
        } else {
            input
        };
        write(GPIO, if output == 1 { 8 } else { 12 }, 1 << 13);
        let actual = (read(GPIO, 4) >> 13) & 1;
        let loopback = (read(GPIO, 0x10) >> 14) & 1;
        let mut line = *b"SIM_NRF_IO_SAMPLE 00000000 I=0 O=0 L=0 T=00000000 V=00000000\r\n";
        hex(&mut line, 18, sequence);
        line[29] = b'0' + input as u8;
        line[33] = b'0' + actual as u8;
        line[37] = b'0' + loopback as u8;
        hex(&mut line, 41, captured);
        hex(&mut line, 52, vector);
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
        write(GPIO, 0x200 + 11 * 4, 0); // input connected, no pull/sense
        write(GPIO, 0x200 + 13 * 4, 3); // output, input disconnected
        write(GPIO, 0x200 + 14 * 4, 0); // modeled output loopback input
        send(b"SIM_NRF_IO_READY v1\r\n");
        send(concat!("SIM_NRF_IO_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(TIMER, 0x004, 1);
        write(TIMER, 0x00c, 1);
        write(TIMER, 0x504, 0); // timer mode; avoid unmodeled BITMODE/SHORTS registers
        write(TIMER, 0x510, if cfg!(sim_slow_timer) { 7 } else { 6 }); // 16 MHz / 64
        write(TIMER, 0x540, 1250); // 5 ms; all captures stay below 16-bit rollover
        write(TIMER, 0x304, 1 << 16);
        if !cfg!(sim_missing_irq) {
            write_volatile(0xe000_e100 as *mut u32, 1 << 8);
        }
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

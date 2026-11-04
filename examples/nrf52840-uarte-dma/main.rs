#![no_main]
#![no_std]

use core::arch::{asm, global_asm};
use core::panic::PanicInfo;
use core::ptr::{read_volatile, write_volatile};

global_asm!(r#"
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
"#);

const UART: usize = 0x4000_2000;
const DMA: usize = 0x4002_8000;
const BUFFER: usize = 0x2000_1004;

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
            while read(UART, 0x11c) == 0 { asm!("nop", options(nomem, nostack)); }
        }
    }
}
fn hex(line: &mut [u8], offset: usize, value: u32) {
    for index in 0..8 {
        line[offset + index] = b"0123456789ABCDEF"[((value >> ((7 - index) * 4)) & 15) as usize];
    }
}
unsafe fn rearm() {
    unsafe {
        write(BUFFER - 4, 0, 0x1357_9bdf);
        write(BUFFER, 0, 0xaaaa_aaaa);
        write(BUFFER + 4, 0, 0x2468_ace0);
        write(DMA, 0x110, 0);
        write(DMA, 0x108, 0);
        write(DMA, 0x14c, 0);
        write(DMA, 0, 1);
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        write(UART, 0x500, 4);
        write(UART, 0x524, 0x01d7_e000);
        write(UART, 0x008, 1);
        send(b"SIM_NRF_UARTE_READY v1\r\n");
        send(concat!("SIM_NRF_UARTE_BUILD ", env!("SIM_BUILD_ID"), "\r\n").as_bytes());
        #[cfg(sim_unmapped_write)]
        write_volatile(0x4000_0000 as *mut u32, 1);
        write(DMA, 0x500, 8);
        write(DMA, 0x534, (BUFFER + usize::from(cfg!(sim_wrong_pointer))) as u32);
        write(DMA, 0x538, if cfg!(sim_wrong_length) { 3 } else { 4 });
        rearm();
    }
    let mut sequence = 0;
    loop {
        unsafe {
            if read(DMA, 0x110) == 0 { continue; }
            sequence += 1;
            let actual = read(BUFFER, 0);
            let word = if cfg!(sim_stale_buffer) { 0xaaaa_aaaa }
                       else if cfg!(sim_wrong_word_order) { actual.swap_bytes() }
                       else { actual };
            let mut line = *b"SIM_NRF_UARTE_SAMPLE 00000000 WORD=00000000 N=00000000 LO=00000000 HI=00000000\r\n";
            hex(&mut line, 21, sequence);
            hex(&mut line, 35, word);
            hex(&mut line, 46, read(DMA, 0x53c));
            hex(&mut line, 58, read(BUFFER - 4, 0));
            hex(&mut line, 70, read(BUFFER + 4, 0));
            send(&line);
            if cfg!(sim_missing_rearm) && sequence == 4 {
                loop { asm!("nop", options(nomem, nostack)); }
            }
            rearm();
        }
    }
}

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    loop { unsafe { asm!("nop", options(nomem, nostack)); } }
}

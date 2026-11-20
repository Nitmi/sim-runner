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
const ECB: usize = 0x4000_e000;
const CONTEXT: usize = 0x2000_1004;
const ALTERNATE: usize = 0x2000_1104;
const MAILBOX: usize = 0x2000_1200;
const KEY: [u8; 16] = [0x2b,0x7e,0x15,0x16,0x28,0xae,0xd2,0xa6,0xab,0xf7,0x15,0x88,0x09,0xcf,0x4f,0x3c];
const CLEAR: [[u8; 16]; 4] = [
    [0x6b,0xc1,0xbe,0xe2,0x2e,0x40,0x9f,0x96,0xe9,0x3d,0x7e,0x11,0x73,0x93,0x17,0x2a],
    [0xae,0x2d,0x8a,0x57,0x1e,0x03,0xac,0x9c,0x9e,0xb7,0x6f,0xac,0x45,0xaf,0x8e,0x51],
    [0x30,0xc8,0x1c,0x46,0xa3,0x5c,0xe4,0x11,0xe5,0xfb,0xc1,0x19,0x1a,0x0a,0x52,0xef],
    [0xf6,0x9f,0x24,0x45,0xdf,0x4f,0x9b,0x17,0xad,0x2b,0x41,0x7b,0xe6,0x6c,0x37,0x10],
];
unsafe fn wr(addr: usize, value: u32) { unsafe { write_volatile(addr as *mut u32,value) }; }
unsafe fn rd(addr: usize) -> u32 { unsafe { read_volatile(addr as *const u32) } }
unsafe fn send(bytes: &[u8]) {
    for &byte in bytes {
        unsafe {
            wr(UART+0x11c,0); wr(UART+0x51c,u32::from(byte));
            while rd(UART+0x11c)==0 { asm!("nop",options(nomem,nostack)); }
        }
    }
}
fn hex(value: u32) -> [u8; 8] {
    let mut out=[0;8];
    for i in 0..8 { out[i]=b"0123456789ABCDEF"[((value>>((7-i)*4))&15) as usize]; }
    out
}
unsafe fn prepare(index: usize) {
    unsafe {
        for (base, vector) in [(CONTEXT,index%4),(ALTERNATE,(index+1)%4)] {
            wr(base-4,0x1357_9bdf);wr(base+48,0x2468_ace0);
            for i in 0..16 {
                let key=KEY[i] ^ u8::from(cfg!(sim_wrong_key) && base==CONTEXT && i==0);
                let clear=CLEAR[vector][i] ^ u8::from(cfg!(sim_wrong_plaintext) && base==CONTEXT && i==0);
                write_volatile((base+i) as *mut u8,key);
                write_volatile((base+16+i) as *mut u8,clear);
                write_volatile((base+32+i) as *mut u8,0xaa);
            }
        }
        if !(cfg!(sim_missing_clear) && index>0) { wr(ECB+0x100,0); }
        wr(ECB+0x504,if cfg!(sim_wrong_pointer) { ALTERNATE as u32 } else { CONTEXT as u32 });
        wr(MAILBOX,0);
    }
}
#[unsafe(no_mangle)]
pub extern "C" fn rust_main() -> ! {
    unsafe {
        wr(UART+0x500,4);wr(UART+0x524,0x01d7_e000);wr(UART+0x008,1);
        send(b"SIM_NRF_ECB_READY v1\r\n");
        send(concat!("SIM_NRF_ECB_BUILD ",env!("SIM_BUILD_ID"),"\r\n").as_bytes());
        #[cfg(sim_unmapped_write)] wr(0x4000_0000,1);
        prepare(0);
    }
    let mut index=0;
    loop {
        unsafe {
            while rd(MAILBOX)!=1 { asm!("nop",options(nomem,nostack)); }
            if !cfg!(sim_missing_start) { wr(ECB,1); }
            while rd(ECB+0x100)==0 { asm!("nop",options(nomem,nostack)); }
            wr(MAILBOX,2);
            while rd(MAILBOX)!=3 { asm!("nop",options(nomem,nostack)); }
            send(b"SIM_NRF_ECB_SAMPLE ");send(&hex(index+1));send(b" OUT=");
            for i in 0..16 {
                let offset=if cfg!(sim_wrong_word_order) { (i/4)*4+3-i%4 } else { i };
                let v=read_volatile((CONTEXT+32+offset) as *const u8);
                send(&[b"0123456789ABCDEF"[(v>>4) as usize],b"0123456789ABCDEF"[(v&15) as usize]]);
            }
            send(b" END=");send(&hex(rd(ECB+0x100)));
            send(b" LO=");send(&hex(rd(CONTEXT-4)));
            send(b" HI=");send(&hex(rd(CONTEXT+48)));send(b"\r\n");
            index+=1;prepare(index as usize);
        }
    }
}
#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! { loop { unsafe { asm!("nop",options(nomem,nostack)); } } }

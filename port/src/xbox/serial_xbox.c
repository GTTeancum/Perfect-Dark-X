// Debug UART output for the Xbox build.
//
// NXDK's debugPrint() only ever writes to the HAL framebuffer, and BOOT_PRINT
// clears the screen before every line, so at most one line is ever visible and
// nothing survives once pbkit takes the display over. That makes early boot
// failures invisible. This writes the same text to COM1 (0x3F8) so XEMU can
// capture it with:
//
//     xemu ... -device lpc47m157 -serial file:<path>
//
// Port I/O is done with inline asm rather than an NXDK helper so this has no
// dependencies beyond the compiler.

#include "serial_xbox.h"

#define COM1 0x3F8

#define UART_DATA        0   // (DLAB=0) data
#define UART_IER         1   // (DLAB=0) interrupt enable / (DLAB=1) divisor hi
#define UART_FCR         2   // FIFO control
#define UART_LCR         3   // line control
#define UART_MCR         4   // modem control
#define UART_LSR         5   // line status

#define LSR_THR_EMPTY    0x20

static int serialReady = 0;

static inline void outb(unsigned short port, unsigned char val)
{
    __asm__ volatile ("outb %0, %1" :: "a"(val), "Nd"(port));
}

static inline unsigned char inb(unsigned short port)
{
    unsigned char ret;
    __asm__ volatile ("inb %1, %0" : "=a"(ret) : "Nd"(port));
    return ret;
}

void serialInit(void)
{
    outb(COM1 + UART_IER, 0x00);   // interrupts off
    outb(COM1 + UART_LCR, 0x80);   // DLAB on
    outb(COM1 + UART_DATA, 0x01);  // divisor 1 => 115200 baud
    outb(COM1 + UART_IER,  0x00);
    outb(COM1 + UART_LCR, 0x03);   // 8N1, DLAB off
    outb(COM1 + UART_FCR, 0xC7);   // FIFO on, clear, 14-byte threshold
    outb(COM1 + UART_MCR, 0x0B);   // DTR + RTS + OUT2
    serialReady = 1;
}

void serialPutc(char c)
{
    if (!serialReady) {
        return;
    }

    // Bounded spin: if nothing is listening the THR still empties, but never
    // hang the boot on a wedged UART.
    for (int i = 0; i < 100000; ++i) {
        if (inb(COM1 + UART_LSR) & LSR_THR_EMPTY) {
            break;
        }
    }

    if (c == '\n') {
        outb(COM1 + UART_DATA, '\r');
        for (int i = 0; i < 100000; ++i) {
            if (inb(COM1 + UART_LSR) & LSR_THR_EMPTY) {
                break;
            }
        }
    }

    outb(COM1 + UART_DATA, (unsigned char)c);
}

void serialPuts(const char *s)
{
    if (!s) {
        return;
    }
    while (*s) {
        serialPutc(*s++);
    }
}

// PD_DBGMARK trace. Emitting on every call would flood the UART from hot
// paths, so only transitions are reported -- that still gives an exact
// execution trace through the marks the codebase already contains.
extern volatile unsigned int g_DbgMark;   // defined in main_xbox.c

void serialMark(unsigned int n)
{
    static unsigned int last = 0xFFFFFFFFu;
    g_DbgMark = n;
    if (n == last) {
        return;
    }
    last = n;

    char b[32];
    int i = 0;
    b[i++] = 'M'; b[i++] = 'K'; b[i++] = ' ';
    if (n == 0) {
        b[i++] = '0';
    } else {
        char d[12]; int j = 0;
        while (n && j < 12) { d[j++] = (char)('0' + (n % 10u)); n /= 10u; }
        while (j) b[i++] = d[--j];
    }
    b[i++] = 10;
    b[i] = 0;
    serialPuts(b);
}

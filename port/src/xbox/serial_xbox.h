#ifndef PORT_XBOX_SERIAL_H
#define PORT_XBOX_SERIAL_H

// COM1 debug output; see serial_xbox.c for why this exists.
#ifdef __cplusplus
extern "C" {
#endif

void serialInit(void);
void serialPutc(char c);
void serialPuts(const char *s);
void serialMark(unsigned int n);

#ifdef __cplusplus
}
#endif

#endif

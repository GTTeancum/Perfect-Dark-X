// Xbox POSIX compat — functions not provided by NXDK's pdclib
#include <ctype.h>

int strcasecmp(const char *a, const char *b)
{
	while (*a && *b) {
		int diff = tolower((unsigned char)*a) - tolower((unsigned char)*b);
		if (diff) return diff;
		a++; b++;
	}
	return tolower((unsigned char)*a) - tolower((unsigned char)*b);
}

int strncasecmp(const char *a, const char *b, unsigned int n)
{
	while (n && *a && *b) {
		int diff = tolower((unsigned char)*a) - tolower((unsigned char)*b);
		if (diff) return diff;
		a++; b++; n--;
	}
	return n ? (tolower((unsigned char)*a) - tolower((unsigned char)*b)) : 0;
}

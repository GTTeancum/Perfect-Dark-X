#ifndef PDX_ISSUE_PROBE_XBOX_H
#define PDX_ISSUE_PROBE_XBOX_H
#include <ultra64.h>
void pdxIssueProbeTick(void);
s32 pdxIssueProbeInputEnabled(void);
s32 pdxIssueProbeReadPad(s32 idx, OSContPad *pad);
void pdxIssueProbeMenuSwipe(s32 direction, s32 outgoingstate);
#endif

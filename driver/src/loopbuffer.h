/*++

Module Name:
    loopbuffer.h

Abstract:
    The loopback ring buffer is the heart of the virtual cable: a single shared
    PCM ring that the render (speaker) stream writes into and the capture
    (microphone) stream reads from.  This is the kernel-mode equivalent of what
    a hardware loopback cable would do.

    The buffer is single-producer / single-consumer.  Reads and writes are
    serialized with a spin lock because PortCls stream callbacks can run at
    DISPATCH_LEVEL.  When the consumer outruns the producer the ring returns
    silence (so an idle virtual mic is quiet, not garbage); when the producer
    outruns the consumer the oldest data is dropped.

Environment:
    Kernel mode.

--*/

#ifndef _DISCROD_LOOPBUFFER_H_
#define _DISCROD_LOOPBUFFER_H_

#include "common.h"

class CLoopbackBuffer
{
public:
    CLoopbackBuffer();
    ~CLoopbackBuffer();

    // Allocate the backing store.  Returns STATUS_INSUFFICIENT_RESOURCES on OOM.
    NTSTATUS Init(_In_ ULONG byteCount);
    void     Free();

    // Producer side (render stream): copy up to byteCount bytes in.
    void     Write(_In_reads_bytes_(byteCount) const PVOID src, _In_ ULONG byteCount);

    // Consumer side (capture stream): copy byteCount bytes out, zero-filling on
    // underrun so the microphone signal stays continuous.
    void     Read(_Out_writes_bytes_(byteCount) PVOID dst, _In_ ULONG byteCount);

    // Drop all buffered audio (called on stream stop).
    void     Reset();

    ULONG    Capacity() const { return m_capacity; }

private:
    PUCHAR   m_data;        // ring storage
    ULONG    m_capacity;    // total bytes
    ULONG    m_writePos;    // producer cursor
    ULONG    m_readPos;     // consumer cursor
    ULONG    m_count;       // valid bytes available to read
    KSPIN_LOCK m_lock;
};

#endif // _DISCROD_LOOPBUFFER_H_

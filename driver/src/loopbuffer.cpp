/*++

Module Name:
    loopbuffer.cpp

Abstract:
    Implementation of the single-producer/single-consumer loopback ring shared
    between the render and capture endpoints.  See loopbuffer.h.

Environment:
    Kernel mode.

--*/

#include "loopbuffer.h"

#pragma code_seg("PAGE")

CLoopbackBuffer::CLoopbackBuffer()
    : m_data(nullptr), m_capacity(0), m_writePos(0), m_readPos(0), m_count(0)
{
    PAGED_CODE();
    KeInitializeSpinLock(&m_lock);
}

CLoopbackBuffer::~CLoopbackBuffer()
{
    PAGED_CODE();
    Free();
}

NTSTATUS CLoopbackBuffer::Init(_In_ ULONG byteCount)
{
    PAGED_CODE();

    Free();

    // Non-paged: touched from DISPATCH_LEVEL stream callbacks.
    m_data = static_cast<PUCHAR>(
        ExAllocatePool2(POOL_FLAG_NON_PAGED, byteCount, DISCROD_POOLTAG));
    if (m_data == nullptr)
    {
        return STATUS_INSUFFICIENT_RESOURCES;
    }

    m_capacity = byteCount;
    m_writePos = 0;
    m_readPos = 0;
    m_count = 0;
    return STATUS_SUCCESS;
}

void CLoopbackBuffer::Free()
{
    PAGED_CODE();
    if (m_data != nullptr)
    {
        ExFreePoolWithTag(m_data, DISCROD_POOLTAG);
        m_data = nullptr;
    }
    m_capacity = m_writePos = m_readPos = m_count = 0;
}

#pragma code_seg()  // the hot paths below must not be pageable

_Use_decl_annotations_
void CLoopbackBuffer::Write(const PVOID src, ULONG byteCount)
{
    if (m_data == nullptr || byteCount == 0)
    {
        return;
    }

    KIRQL oldIrql;
    KeAcquireSpinLock(&m_lock, &oldIrql);

    const PUCHAR in = static_cast<PUCHAR>(src);

    // If asked to write more than the ring holds, keep only the newest bytes.
    ULONG toWrite = byteCount;
    ULONG offset = 0;
    if (toWrite > m_capacity)
    {
        offset = toWrite - m_capacity;
        toWrite = m_capacity;
    }

    ULONG firstChunk = min(toWrite, m_capacity - m_writePos);
    RtlCopyMemory(m_data + m_writePos, in + offset, firstChunk);
    if (toWrite > firstChunk)
    {
        RtlCopyMemory(m_data, in + offset + firstChunk, toWrite - firstChunk);
    }

    m_writePos = (m_writePos + toWrite) % m_capacity;
    m_count += toWrite;
    if (m_count > m_capacity)
    {
        // Overrun: advance read cursor, dropping oldest audio.
        ULONG drop = m_count - m_capacity;
        m_readPos = (m_readPos + drop) % m_capacity;
        m_count = m_capacity;
    }

    KeReleaseSpinLock(&m_lock, oldIrql);
}

_Use_decl_annotations_
void CLoopbackBuffer::Read(PVOID dst, ULONG byteCount)
{
    PUCHAR out = static_cast<PUCHAR>(dst);

    if (m_data == nullptr)
    {
        RtlZeroMemory(out, byteCount);
        return;
    }

    KIRQL oldIrql;
    KeAcquireSpinLock(&m_lock, &oldIrql);

    ULONG avail = min(byteCount, m_count);
    if (avail > 0)
    {
        ULONG firstChunk = min(avail, m_capacity - m_readPos);
        RtlCopyMemory(out, m_data + m_readPos, firstChunk);
        if (avail > firstChunk)
        {
            RtlCopyMemory(out + firstChunk, m_data, avail - firstChunk);
        }
        m_readPos = (m_readPos + avail) % m_capacity;
        m_count -= avail;
    }

    // Underrun: pad the remainder with silence so the mic stream is continuous.
    if (avail < byteCount)
    {
        RtlZeroMemory(out + avail, byteCount - avail);
    }

    KeReleaseSpinLock(&m_lock, oldIrql);
}

void CLoopbackBuffer::Reset()
{
    KIRQL oldIrql;
    KeAcquireSpinLock(&m_lock, &oldIrql);
    m_writePos = m_readPos = m_count = 0;
    KeReleaseSpinLock(&m_lock, oldIrql);
}

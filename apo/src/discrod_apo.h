// DiscrodCaptureApo — the COM object Windows loads into the capture endpoint's
// audio processing chain.
//
// It implements the three interfaces a software APO needs:
//   * IAudioProcessingObject          — discovery + format negotiation
//   * IAudioProcessingObjectConfiguration — Lock/UnlockForProcess lifecycle
//   * IAudioProcessingObjectRT        — the real-time APOProcess call
//
// All signal work is delegated to the platform-independent ApoCore; this class
// is COM plumbing plus opening the shared-memory bridge file.  The design
// follows the Windows "sysvad" SWAP APO sample, adapted for a capture (MFX)
// effect that injects the app's DSP + soundboard mix onto the real mic.

#pragma once

#ifdef _WIN32

#include <windows.h>
#include <audioengineendpoint.h>
#include <audioenginebaseapo.h>
#include <audioapotypes.h>

#include "apo_core.h"

class DiscrodCaptureApo : public IAudioProcessingObjectRT,
                          public IAudioProcessingObject,
                          public IAudioProcessingObjectConfiguration {
public:
    DiscrodCaptureApo();
    virtual ~DiscrodCaptureApo();

    // IUnknown
    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) override;
    STDMETHOD_(ULONG, AddRef)() override;
    STDMETHOD_(ULONG, Release)() override;

    // IAudioProcessingObject
    STDMETHOD(Reset)() override;
    STDMETHOD(GetLatency)(HNSTIME* pTime) override;
    STDMETHOD(GetRegistrationProperties)(APO_REG_PROPERTIES** ppProps) override;
    STDMETHOD(Initialize)(UINT32 cbDataSize, BYTE* pbData) override;
    STDMETHOD(IsInputFormatSupported)(IAudioMediaType* pOppositeFormat,
                                      IAudioMediaType* pRequestedInputFormat,
                                      IAudioMediaType** ppSupportedInputFormat) override;
    STDMETHOD(IsOutputFormatSupported)(IAudioMediaType* pOppositeFormat,
                                       IAudioMediaType* pRequestedOutputFormat,
                                       IAudioMediaType** ppSupportedOutputFormat) override;
    STDMETHOD(GetInputChannelCount)(UINT32* pu32ChannelCount) override;

    // IAudioProcessingObjectConfiguration
    STDMETHOD(LockForProcess)(UINT32 u32NumInputConnections,
                              APO_CONNECTION_DESCRIPTOR** ppInputConnections,
                              UINT32 u32NumOutputConnections,
                              APO_CONNECTION_DESCRIPTOR** ppOutputConnections) override;
    STDMETHOD(UnlockForProcess)() override;

    // IAudioProcessingObjectRT
    STDMETHOD_(void, APOProcess)(UINT32 u32NumInputConnections,
                                 APO_CONNECTION_PROPERTY** ppInputConnections,
                                 UINT32 u32NumOutputConnections,
                                 APO_CONNECTION_PROPERTY** ppOutputConnections) override;
    STDMETHOD_(UINT32, CalcInputFrames)(UINT32 u32OutputFrameCount) override;
    STDMETHOD_(UINT32, CalcOutputFrames)(UINT32 u32InputFrameCount) override;

private:
    HRESULT ValidateFormat(IAudioMediaType* fmt, UINT32* sampleRate,
                           UINT32* channels);
    void OpenBridge();
    void CloseBridge();

    LONG m_ref;
    bool m_locked;
    UINT32 m_sampleRate;
    UINT32 m_channels;
    UINT32 m_frameCount;   // frames per connection, from LockForProcess

    HANDLE m_file;
    HANDLE m_mapping;
    void* m_view;
    UINT32 m_viewSize;

    discrod::ApoCore m_core;
};

#endif  // _WIN32

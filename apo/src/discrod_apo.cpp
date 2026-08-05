#ifdef _WIN32

#include "discrod_apo.h"

#include <ks.h>
#include <ksmedia.h>
#include <mmreg.h>
#include <new>
#include <shlobj.h>

#include "apo_guids.h"

// The bridge file lives under %ProgramData%\Discrod so both the user-session
// app and the audiodg service can reach it (see bridge_protocol.h).
static const wchar_t kBridgeRelPath[] = L"\\Discrod\\apo_bridge.bin";

// --- registration properties ------------------------------------------------
//
// A capture-mode effect (APO_FLAG_MFX) that runs framework-registered.  We
// report samples-processed=0 latency and declare float32 as the only supported
// format so the engine hands us the shape ApoCore expects.

static const APO_REG_PROPERTIES g_RegProps = {
    /* clsid            */ CLSID_DiscrodCaptureApo,
    /* Flags            */ APO_FLAG_INPLACE,
    /* szFriendlyName   */ L"Discrod Capture Effect",
    /* szCopyrightInfo  */ L"Discrod",
    /* u32MajorVersion  */ 1,
    /* u32MinorVersion  */ 0,
    /* u32MinInputConnections  */ 1,
    /* u32MaxInputConnections  */ 1,
    /* u32MinOutputConnections */ 1,
    /* u32MaxOutputConnections */ 1,
    /* u32MaxInstances         */ 0,
    /* u32NumAPOInterfaces     */ 1,
    /* iidAPOInterfaceList     */ {__uuidof(IAudioProcessingObject)},
};

DiscrodCaptureApo::DiscrodCaptureApo()
    : m_ref(1), m_locked(false), m_sampleRate(0), m_channels(0),
      m_frameCount(0), m_file(INVALID_HANDLE_VALUE), m_mapping(nullptr),
      m_view(nullptr), m_viewSize(0) {}

DiscrodCaptureApo::~DiscrodCaptureApo() { CloseBridge(); }

// --- IUnknown ---------------------------------------------------------------

STDMETHODIMP DiscrodCaptureApo::QueryInterface(REFIID riid, void** ppv) {
    if (ppv == nullptr) return E_POINTER;
    if (riid == __uuidof(IUnknown) ||
        riid == __uuidof(IAudioProcessingObject)) {
        *ppv = static_cast<IAudioProcessingObject*>(this);
    } else if (riid == __uuidof(IAudioProcessingObjectRT)) {
        *ppv = static_cast<IAudioProcessingObjectRT*>(this);
    } else if (riid == __uuidof(IAudioProcessingObjectConfiguration)) {
        *ppv = static_cast<IAudioProcessingObjectConfiguration*>(this);
    } else {
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG) DiscrodCaptureApo::AddRef() {
    return InterlockedIncrement(&m_ref);
}

STDMETHODIMP_(ULONG) DiscrodCaptureApo::Release() {
    LONG r = InterlockedDecrement(&m_ref);
    if (r == 0) delete this;
    return r;
}

// --- IAudioProcessingObject -------------------------------------------------

STDMETHODIMP DiscrodCaptureApo::Reset() {
    m_core.detach();
    if (m_view != nullptr)
        m_core.attach(m_view, m_viewSize, m_sampleRate, m_channels);
    return S_OK;
}

STDMETHODIMP DiscrodCaptureApo::GetLatency(HNSTIME* pTime) {
    if (pTime == nullptr) return E_POINTER;
    *pTime = 0;  // in-place, no added algorithmic delay
    return S_OK;
}

STDMETHODIMP DiscrodCaptureApo::GetRegistrationProperties(
    APO_REG_PROPERTIES** ppProps) {
    if (ppProps == nullptr) return E_POINTER;
    auto* p = static_cast<APO_REG_PROPERTIES*>(
        CoTaskMemAlloc(sizeof(APO_REG_PROPERTIES)));
    if (p == nullptr) return E_OUTOFMEMORY;
    *p = g_RegProps;
    *ppProps = p;
    return S_OK;
}

STDMETHODIMP DiscrodCaptureApo::Initialize(UINT32 cbDataSize, BYTE* pbData) {
    UNREFERENCED_PARAMETER(cbDataSize);
    UNREFERENCED_PARAMETER(pbData);
    return S_OK;
}

HRESULT DiscrodCaptureApo::ValidateFormat(IAudioMediaType* fmt,
                                          UINT32* sampleRate,
                                          UINT32* channels) {
    if (fmt == nullptr) return E_POINTER;
    const WAVEFORMATEX* wfx = nullptr;
    HRESULT hr = fmt->GetAudioFormat(&wfx);
    if (FAILED(hr) || wfx == nullptr) return APOERR_FORMAT_NOT_SUPPORTED;

    // Accept IEEE float (or the float subtype under WAVE_FORMAT_EXTENSIBLE).
    bool isFloat = wfx->wFormatTag == WAVE_FORMAT_IEEE_FLOAT;
    if (wfx->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        auto* ext = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wfx);
        isFloat = ext->SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT;
    }
    if (!isFloat || wfx->wBitsPerSample != 32)
        return APOERR_FORMAT_NOT_SUPPORTED;
    if (wfx->nChannels < 1 || wfx->nChannels > discrod::RING_MAX_CHANNELS)
        return APOERR_FORMAT_NOT_SUPPORTED;

    if (sampleRate) *sampleRate = wfx->nSamplesPerSec;
    if (channels) *channels = wfx->nChannels;
    return S_OK;
}

STDMETHODIMP DiscrodCaptureApo::IsInputFormatSupported(
    IAudioMediaType* pOppositeFormat, IAudioMediaType* pRequestedInputFormat,
    IAudioMediaType** ppSupportedInputFormat) {
    UNREFERENCED_PARAMETER(pOppositeFormat);
    if (pRequestedInputFormat == nullptr) return E_POINTER;
    HRESULT hr = ValidateFormat(pRequestedInputFormat, nullptr, nullptr);
    if (ppSupportedInputFormat != nullptr) {
        *ppSupportedInputFormat = SUCCEEDED(hr) ? pRequestedInputFormat : nullptr;
        if (SUCCEEDED(hr)) pRequestedInputFormat->AddRef();
    }
    return SUCCEEDED(hr) ? S_OK : S_FALSE;
}

STDMETHODIMP DiscrodCaptureApo::IsOutputFormatSupported(
    IAudioMediaType* pOppositeFormat, IAudioMediaType* pRequestedOutputFormat,
    IAudioMediaType** ppSupportedOutputFormat) {
    UNREFERENCED_PARAMETER(pOppositeFormat);
    if (pRequestedOutputFormat == nullptr) return E_POINTER;
    HRESULT hr = ValidateFormat(pRequestedOutputFormat, nullptr, nullptr);
    if (ppSupportedOutputFormat != nullptr) {
        *ppSupportedOutputFormat = SUCCEEDED(hr) ? pRequestedOutputFormat : nullptr;
        if (SUCCEEDED(hr)) pRequestedOutputFormat->AddRef();
    }
    return SUCCEEDED(hr) ? S_OK : S_FALSE;
}

STDMETHODIMP DiscrodCaptureApo::GetInputChannelCount(UINT32* pu32ChannelCount) {
    if (pu32ChannelCount == nullptr) return E_POINTER;
    *pu32ChannelCount = m_channels ? m_channels : 2;
    return S_OK;
}

// --- bridge file ------------------------------------------------------------

void DiscrodCaptureApo::OpenBridge() {
    CloseBridge();

    wchar_t base[MAX_PATH];
    if (FAILED(SHGetFolderPathW(nullptr, CSIDL_COMMON_APPDATA, nullptr,
                                SHGFP_TYPE_CURRENT, base)))
        return;
    wchar_t path[MAX_PATH];
    if (wcslen(base) + wcslen(kBridgeRelPath) >= MAX_PATH) return;
    wcscpy_s(path, base);
    wcscat_s(path, kBridgeRelPath);

    // Open existing only: the app owns creation and initialization.  If it is
    // not running yet the APO runs as passthrough and picks the bridge up on
    // the next Reset()/LockForProcess.
    m_file = CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (m_file == INVALID_HANDLE_VALUE) return;

    m_mapping = CreateFileMappingW(m_file, nullptr, PAGE_READWRITE, 0,
                                   discrod::BRIDGE_FILE_SIZE, nullptr);
    if (m_mapping == nullptr) {
        CloseBridge();
        return;
    }
    m_view = MapViewOfFile(m_mapping, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0,
                           discrod::BRIDGE_FILE_SIZE);
    if (m_view == nullptr) {
        CloseBridge();
        return;
    }
    m_viewSize = discrod::BRIDGE_FILE_SIZE;
}

void DiscrodCaptureApo::CloseBridge() {
    m_core.detach();
    if (m_view != nullptr) {
        UnmapViewOfFile(m_view);
        m_view = nullptr;
    }
    if (m_mapping != nullptr) {
        CloseHandle(m_mapping);
        m_mapping = nullptr;
    }
    if (m_file != INVALID_HANDLE_VALUE) {
        CloseHandle(m_file);
        m_file = INVALID_HANDLE_VALUE;
    }
    m_viewSize = 0;
}

// --- IAudioProcessingObjectConfiguration ------------------------------------

STDMETHODIMP DiscrodCaptureApo::LockForProcess(
    UINT32 u32NumInputConnections,
    APO_CONNECTION_DESCRIPTOR** ppInputConnections,
    UINT32 u32NumOutputConnections,
    APO_CONNECTION_DESCRIPTOR** ppOutputConnections) {
    if (u32NumInputConnections != 1 || u32NumOutputConnections != 1)
        return APOERR_NUM_CONNECTIONS_INVALID;
    if (ppInputConnections == nullptr || ppOutputConnections == nullptr)
        return E_POINTER;

    UINT32 rate = 0, channels = 0;
    HRESULT hr = ValidateFormat(ppInputConnections[0]->pFormat, &rate, &channels);
    if (FAILED(hr)) return hr;

    // In-place APO: the framework hands us the same buffer for in and out, and
    // both sides must agree on format.
    UINT32 outRate = 0, outChannels = 0;
    hr = ValidateFormat(ppOutputConnections[0]->pFormat, &outRate, &outChannels);
    if (FAILED(hr)) return hr;
    if (outRate != rate || outChannels != channels)
        return APOERR_FORMAT_NOT_SUPPORTED;

    m_sampleRate = rate;
    m_channels = channels;
    m_frameCount = ppInputConnections[0]->u32MaxFrameCount;

    OpenBridge();
    m_core.attach(m_view, m_viewSize, m_sampleRate, m_channels);
    m_locked = true;
    return S_OK;
}

STDMETHODIMP DiscrodCaptureApo::UnlockForProcess() {
    m_locked = false;
    CloseBridge();
    return S_OK;
}

// --- IAudioProcessingObjectRT (real-time) -----------------------------------

STDMETHODIMP_(void) DiscrodCaptureApo::APOProcess(
    UINT32 u32NumInputConnections,
    APO_CONNECTION_PROPERTY** ppInputConnections,
    UINT32 u32NumOutputConnections,
    APO_CONNECTION_PROPERTY** ppOutputConnections) {
    UNREFERENCED_PARAMETER(u32NumInputConnections);
    UNREFERENCED_PARAMETER(u32NumOutputConnections);
    if (ppInputConnections == nullptr || ppOutputConnections == nullptr) return;

    APO_CONNECTION_PROPERTY* in = ppInputConnections[0];
    APO_CONNECTION_PROPERTY* out = ppOutputConnections[0];

    switch (in->u32BufferFlags) {
        case BUFFER_INVALID:
            out->u32ValidFrameCount = 0;
            out->u32BufferFlags = BUFFER_INVALID;
            return;
        case BUFFER_SILENT:
            // Even on a silent mic block the soundboard can inject audio, so
            // materialize zeros and let the core mix over them.
            std::memset(reinterpret_cast<void*>(in->pBuffer), 0,
                        sizeof(float) * in->u32ValidFrameCount * m_channels);
            break;
        default:
            break;
    }

    auto* buffer = reinterpret_cast<float*>(in->pBuffer);
    m_core.process(buffer, in->u32ValidFrameCount, m_channels);

    // In-place: output shares the input buffer.  Report a non-silent,
    // valid block so the soundboard-only case still reaches Discord.
    out->u32ValidFrameCount = in->u32ValidFrameCount;
    out->u32BufferFlags = BUFFER_VALID;
}

STDMETHODIMP_(UINT32) DiscrodCaptureApo::CalcInputFrames(
    UINT32 u32OutputFrameCount) {
    return u32OutputFrameCount;  // 1:1, no resampling
}

STDMETHODIMP_(UINT32) DiscrodCaptureApo::CalcOutputFrames(
    UINT32 u32InputFrameCount) {
    return u32InputFrameCount;
}

#endif  // _WIN32

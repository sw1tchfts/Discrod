// COM server entry points and class factory for the Discrod capture APO.
//
// Standard in-proc server: DllGetClassObject hands out an IClassFactory for
// CLSID_DiscrodCaptureApo; DllRegisterServer writes the InprocServer32 keys
// (ThreadingModel = Both, per APO requirements) plus the AudioProcessingObjects
// registration the audio service enumerates.  Endpoint wiring — attaching this
// CLSID to the capture endpoint's FX property store — is done by the app's
// PowerShell tool, not here, because it targets a specific device the user
// picks.

#ifdef _WIN32

#include <windows.h>
#include <objbase.h>
#include <olectl.h>
#include <cstdio>
#include <new>

#include "apo_guids.h"
#include "discrod_apo.h"

static HMODULE g_module = nullptr;
static LONG g_lockCount = 0;

// CLSID as a string for registry paths.
static const wchar_t kClsidText[] = L"{7C9D5A24-6B3E-4C1F-9A2D-3E8F1B0C6A50}";
static const wchar_t kFriendly[] = L"Discrod Capture Effect";

// --- class factory ----------------------------------------------------------

class ApoClassFactory : public IClassFactory {
public:
    ApoClassFactory() : m_ref(1) {}

    STDMETHOD(QueryInterface)(REFIID riid, void** ppv) override {
        if (ppv == nullptr) return E_POINTER;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IClassFactory)) {
            *ppv = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    STDMETHOD_(ULONG, AddRef)() override { return InterlockedIncrement(&m_ref); }
    STDMETHOD_(ULONG, Release)() override {
        LONG r = InterlockedDecrement(&m_ref);
        if (r == 0) delete this;
        return r;
    }

    STDMETHOD(CreateInstance)(IUnknown* outer, REFIID riid, void** ppv) override {
        if (ppv == nullptr) return E_POINTER;
        *ppv = nullptr;
        if (outer != nullptr) return CLASS_E_NOAGGREGATION;
        auto* apo = new (std::nothrow) DiscrodCaptureApo();
        if (apo == nullptr) return E_OUTOFMEMORY;
        HRESULT hr = apo->QueryInterface(riid, ppv);
        apo->Release();  // QI took the reference we hand out
        return hr;
    }

    STDMETHOD(LockServer)(BOOL lock) override {
        if (lock) InterlockedIncrement(&g_lockCount);
        else InterlockedDecrement(&g_lockCount);
        return S_OK;
    }

private:
    LONG m_ref;
};

// --- exports ----------------------------------------------------------------

extern "C" BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = inst;
        DisableThreadLibraryCalls(inst);
    }
    return TRUE;
}

extern "C" HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID riid,
                                            void** ppv) {
    if (ppv == nullptr) return E_POINTER;
    *ppv = nullptr;
    if (clsid != CLSID_DiscrodCaptureApo) return CLASS_E_CLASSNOTAVAILABLE;
    auto* factory = new (std::nothrow) ApoClassFactory();
    if (factory == nullptr) return E_OUTOFMEMORY;
    HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release();
    return hr;
}

extern "C" HRESULT WINAPI DllCanUnloadNow() {
    return g_lockCount == 0 ? S_OK : S_FALSE;
}

static LONG SetKeyValue(HKEY root, const wchar_t* subkey, const wchar_t* name,
                        const wchar_t* value) {
    HKEY key = nullptr;
    LONG rc = RegCreateKeyExW(root, subkey, 0, nullptr, 0, KEY_WRITE, nullptr,
                              &key, nullptr);
    if (rc != ERROR_SUCCESS) return rc;
    rc = RegSetValueExW(key, name, 0, REG_SZ,
                        reinterpret_cast<const BYTE*>(value),
                        static_cast<DWORD>((wcslen(value) + 1) * sizeof(wchar_t)));
    RegCloseKey(key);
    return rc;
}

extern "C" HRESULT WINAPI DllRegisterServer() {
    wchar_t modulePath[MAX_PATH];
    if (GetModuleFileNameW(g_module, modulePath, MAX_PATH) == 0)
        return HRESULT_FROM_WIN32(GetLastError());

    wchar_t clsidKey[128];
    swprintf_s(clsidKey, L"CLSID\\%s", kClsidText);
    wchar_t inprocKey[160];
    swprintf_s(inprocKey, L"CLSID\\%s\\InprocServer32", kClsidText);

    if (SetKeyValue(HKEY_CLASSES_ROOT, clsidKey, nullptr, kFriendly) != ERROR_SUCCESS ||
        SetKeyValue(HKEY_CLASSES_ROOT, inprocKey, nullptr, modulePath) != ERROR_SUCCESS ||
        // APOs must be agile — the audio engine loads them on its own threads.
        SetKeyValue(HKEY_CLASSES_ROOT, inprocKey, L"ThreadingModel", L"Both") != ERROR_SUCCESS)
        return SELFREG_E_CLASS;

    // Advertise as an Audio Processing Object so the audio service enumerates
    // it. The per-endpoint FX assignment is done by the app's registration
    // tool against the device the user selects.
    wchar_t apoKey[192];
    swprintf_s(apoKey,
               L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\"
               L"AudioProcessingObjects\\%s", kClsidText);
    SetKeyValue(HKEY_LOCAL_MACHINE, apoKey, L"FriendlyName", kFriendly);
    SetKeyValue(HKEY_LOCAL_MACHINE, apoKey, L"Class", kClsidText);
    return S_OK;
}

extern "C" HRESULT WINAPI DllUnregisterServer() {
    wchar_t clsidKey[128];
    swprintf_s(clsidKey, L"CLSID\\%s", kClsidText);
    RegDeleteTreeW(HKEY_CLASSES_ROOT, clsidKey);

    wchar_t apoKey[192];
    swprintf_s(apoKey,
               L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\"
               L"AudioProcessingObjects\\%s", kClsidText);
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, apoKey);
    return S_OK;
}

#endif  // _WIN32

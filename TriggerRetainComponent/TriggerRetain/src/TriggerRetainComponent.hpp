#pragma once
#include "Arp/System/Core/Arp.h"

// --- NEW SDK 2026.6 MANAGEMENT (ABI V2) ---
#if ARP_ABI_VERSION_MAJOR < 2
#include "Arp/System/Acf/ComponentBase.hpp"
#include "Arp/System/Acf/IApplication.hpp"
#else
#include "Arp/Base/Acf/Commons/ComponentBase.hpp"
#endif

#include "Arp/Plc/Commons/Meta/MetaComponentBase.hpp"
#include "Arp/System/Commons/Threading/Thread.hpp"
#include "Arp/System/Commons/Threading/ThreadSettings.hpp"
#include "Arp/System/Commons/Logging.h"
#include "Arp/Plc/Domain/Services/IPlcManagerService.hpp"
#include "Arp/Plc/Retain/Services/IRetainManagerService.hpp"
#include "Arp/System/Commons/Io/FileStream.hpp"

namespace TriggerRetain
{
    using namespace Arp;
    using namespace Arp::System::Commons::Threading;
    using namespace Arp::Plc::Domain::Services;
    using namespace Arp::Plc::Retain::Services;
    using namespace Arp::Plc::Commons::Meta;

    // --- ABI V2 NAMESPACE MANAGEMENT ---
#if ARP_ABI_VERSION_MAJOR < 2
    using namespace Arp::System::Acf;
#else
    using namespace Arp::Base::Acf::Commons;
#endif

    class TriggerRetainComponent : public ComponentBase, public MetaComponentBase, private Loggable<TriggerRetainComponent>
    {
    public: // construction/destruction
#if ARP_ABI_VERSION_MAJOR < 2
        TriggerRetainComponent(IApplication& application, const String& name);
        virtual ~TriggerRetainComponent() = default;
#else
        TriggerRetainComponent(ILibrary& library, const String& name);
#endif

    public: // IComponent operations
        void Initialize() override;
        void LoadConfig() override;
        void SetupConfig() override;
        void ResetConfig() override;
        void Dispose() override;
        void PowerDown() override;
        void LoadSettings(const String& settingsPath) override;
        void SetupSettings() override;
        void SubscribeServices() override;
        void PublishServices() override;

        void Start();
        void Stop();

    private: // methods
#if ARP_ABI_VERSION_MAJOR < 2
        TriggerRetainComponent(const TriggerRetainComponent& arg) = delete;
        TriggerRetainComponent& operator= (const TriggerRetainComponent& arg) = delete;

    public: // static factory operations
        static IComponent::Ptr Create(Arp::System::Acf::IApplication& application, const String& name);
#endif

    private:
        void OnPlcLoaded();
        void OnPlcStopping();
        void retainThreadBody(void* pParameter);

        String ReadFromFile(String filePath);
        void WriteToFile(String filePath, String textToWrite);
        bool CheckSaveBackup();
        bool CheckRestoreBackup();
        void SaveBackup();
        void RestoreBackup();

    private: // fields
        IRetainManagerService::Ptr retainSvc;
        IPlcManagerService::Ptr plcSvc;
        Thread retainThreadInstance;
        bool isRunning = false;
        bool restore = false;
        int tmp = 0;
    };

#if ARP_ABI_VERSION_MAJOR < 2
    inline IComponent::Ptr TriggerRetainComponent::Create(Arp::System::Acf::IApplication& application, const String& name)
    {
        return IComponent::Ptr(new TriggerRetainComponent(application, name));
    }
#endif

}

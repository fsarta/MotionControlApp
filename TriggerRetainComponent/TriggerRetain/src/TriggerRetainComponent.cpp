#include "TriggerRetainComponent.hpp"
#include "Arp/Plc/Commons/Domain/PlcDomainProxy.hpp"
#include "TriggerRetainLibrary.hpp"
#include "Arp/System/Rsc/ServiceManager.hpp"
#include "Arp/System/Commons/Io/FileStream.hpp"
#include <cstdio>
#include "Arp/System/Commons/Io/File.hpp"

namespace TriggerRetain
{
    using namespace Arp::Plc::Commons::Domain;
    using Arp::System::Rsc::ServiceManager;
    using namespace Arp::System::Commons::Io;

    // --- COSTRUTTORE ADATTIVO (ABI V1 / V2) ---
#if ARP_ABI_VERSION_MAJOR < 2
    TriggerRetainComponent::TriggerRetainComponent(IApplication& application, const String& name)
        : ComponentBase(application, ::TriggerRetain::TriggerRetainLibrary::GetInstance(), name, ComponentCategory::Custom)
        , MetaComponentBase(::TriggerRetain::TriggerRetainLibrary::GetInstance().GetNamespace())
        , retainThreadInstance(ThreadSettings("RetainThread", 20, 0, 0), this, &TriggerRetainComponent::retainThreadBody, (void*)&tmp)
#else
    TriggerRetainComponent::TriggerRetainComponent(ILibrary& library, const String& name)
        : ComponentBase(library, name, ComponentCategory::Custom, GetDefaultStartOrder())
        , MetaComponentBase(::TriggerRetain::TriggerRetainLibrary::GetInstance().GetNamespace())
        , retainThreadInstance(ThreadSettings("RetainThread", 20, 0, 0), this, &TriggerRetainComponent::retainThreadBody, (void*)&tmp)
#endif
    {
    }

    void TriggerRetainComponent::Initialize()
    {
        PlcDomainProxy::GetInstance().RegisterComponent(*this, true);

        PlcDomainProxy& plcDomainProxy = PlcDomainProxy::GetInstance();
        plcDomainProxy.PlcLoaded += make_delegate(this, &TriggerRetainComponent::OnPlcLoaded);
        plcDomainProxy.PlcStopping += make_delegate(this, &TriggerRetainComponent::OnPlcStopping);
    }

    void TriggerRetainComponent::SubscribeServices()
    {
        retainSvc = ServiceManager::GetService<IRetainManagerService>();
        plcSvc = ServiceManager::GetService<IPlcManagerService>();
    }

    void TriggerRetainComponent::LoadSettings(const String& /*settingsPath*/) {}
    void TriggerRetainComponent::SetupSettings()
    {
        MetaComponentBase::SetupSettings();
    }

    void TriggerRetainComponent::PublishServices() {}
    void TriggerRetainComponent::LoadConfig() {}
    void TriggerRetainComponent::SetupConfig() {}
    void TriggerRetainComponent::ResetConfig() {}

    void TriggerRetainComponent::Dispose()
    {
        MetaComponentBase::Dispose();

        PlcDomainProxy& plcDomainProxy = PlcDomainProxy::GetInstance();
        plcDomainProxy.PlcLoaded -= make_delegate(this, &TriggerRetainComponent::OnPlcLoaded);
        plcDomainProxy.PlcStopping -= make_delegate(this, &TriggerRetainComponent::OnPlcStopping);
    }

    void TriggerRetainComponent::PowerDown() {}

    void TriggerRetainComponent::OnPlcLoaded()
    {
        restore = CheckRestoreBackup();
    }

    void TriggerRetainComponent::OnPlcStopping()
    {
        if (CheckSaveBackup()) {
            std::remove("/opt/plcnext/user/BackupAction.txt");
            SaveBackup();
        }
    }

    void TriggerRetainComponent::Start(void) {
        isRunning = true;
        retainThreadInstance.Start();
    }

    void TriggerRetainComponent::Stop(void) {
        isRunning = false;
        retainThreadInstance.Interrupt();
        if (retainThreadInstance.IsJoinable()) {
            retainThreadInstance.Join();
        }
    }

    void TriggerRetainComponent::retainThreadBody(void* pParameter)
    {
        while (isRunning)
        {
            if (File::Exists("/opt/plcnext/user/RestartAction.txt"))
            {
                String action = ReadFromFile("/opt/plcnext/user/RestartAction.txt");
                if (action == "restart")
                {
                    std::remove("/opt/plcnext/user/RestartAction.txt");
                    std::system("systemctl --no-block restart plcnext");
                }
            }
            if (restore)
            {
                restore = false;
                // Attende il completamento della transizione di avvio di PlcManager
                Thread::Sleep(2000);
                std::remove("/opt/plcnext/user/BackupAction.txt");
                this->plcSvc->Stop();
                BackupResult result = this->retainSvc->PrepareLatestBackupFileForRestoring();
                if (result.Error == BackupError::None)
                {
                    log.Info("-------------------------------Retain restore prepared: {0}", result.FileName);
                    this->plcSvc->Start(PlcStartKind::RestoreWarm);
                }
                else
                {
                    log.Warning("-------------------------------No valid retain backup ({0}), starting Warm.", result.Error);
                    this->plcSvc->Start(PlcStartKind::Warm);
                }
            }
            Thread::Sleep(1000);
        }
    }

    String TriggerRetainComponent::ReadFromFile(String filePath)
    {
        try {
            if (!File::Exists(filePath)) {
                return "";
            }
            FileStream stream(filePath, FileMode::Open, FileAccess::Read);
            auto length = stream.GetLength();
            if (length <= 0) {
                stream.Close();
                return "";
            }
            String str((String::size_type)length, (String::CharType)'\0');
            if (stream.CanRead()) {
                stream.Read((Arp::byte*)str.CStr(), length, 0, length);
            }
            stream.Close();
            return str;
        }
        catch (const Arp::Exception& e) {
            log.Error("-------------------------------Cannot Read File {0}: {1}", filePath, e.GetMessage());
            return "";
        }
    }

    void TriggerRetainComponent::WriteToFile(String filePath, String textToWrite) {
        try {
            FileStream stream(filePath, FileMode::Create);
            Arp::String str = textToWrite;
            try {
                if (stream.CanWrite())
                    stream.Write((Arp::byte*)str.CStr(), str.Length(), 0, str.Length());
            }
            catch (const Arp::Exception& e) {
                log.Error("--- Cannot Write to File. Error message: {0}.", e.GetMessage());
            }
            stream.Close();
        }
        catch (const Arp::Exception& e) {
            log.Error("--- Cannot Create File:{0}", e.GetMessage());
        }
    }

    bool TriggerRetainComponent::CheckSaveBackup() {
        String tmp = ReadFromFile("/opt/plcnext/user/BackupAction.txt");
        return tmp == "backup";
    }

    bool TriggerRetainComponent::CheckRestoreBackup() {
        String tmp = ReadFromFile("/opt/plcnext/user/BackupAction.txt");
        return tmp == "restore";
    }

    void TriggerRetainComponent::SaveBackup()
    {
        BackupResult result = this->retainSvc->GenerateBackupFile();
        if (result.Error == BackupError::None)
            log.Info("-------------------------------Retain saved: {0}", result.FileName);
        else
            log.Error("-------------------------------Retain save failed: {0}", result.Error);
    }

    void TriggerRetainComponent::RestoreBackup()
    {
        BackupResult result = this->retainSvc->PrepareLatestBackupFileForRestoring();
        if (result.Error == BackupError::None)
            log.Info("-------------------------------Retain restore prepared: {0}", result.FileName);
        else
            log.Error("-------------------------------Retain restore failed: {0}", result.Error);
    }

} // end of namespace TriggerRetain
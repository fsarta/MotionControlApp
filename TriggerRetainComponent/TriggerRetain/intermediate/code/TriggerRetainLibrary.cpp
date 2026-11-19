#include "TriggerRetainLibrary.hpp"
#if ARP_ABI_VERSION_MAJOR < 2
#include "Arp/System/Core/CommonTypeName.hxx"
#else
#include "Arp/Base/Core/CommonTypeName.hxx"
#include "TriggerRetainLibraryInfo.hpp"
#endif
#include "Arp/Plc/Commons/Meta/TypeSystem/TypeSystem.h"

namespace TriggerRetain
{

#if ARP_ABI_VERSION_MAJOR < 2
TriggerRetainLibrary::TriggerRetainLibrary(AppDomain& appDomain)
    : MetaLibraryBase(appDomain, ARP_VERSION_CURRENT, typeDomain)
    , typeDomain(CommonTypeName<TriggerRetainLibrary>().GetNamespace())
#else
TriggerRetainLibrary::TriggerRetainLibrary()
    : MetaLibraryBase(TriggerRetainLibraryVersion, typeDomain)
    , typeDomain(CommonTypeName<TriggerRetainLibrary>().GetNamespace())
#endif
{
#if ARP_ABI_VERSION_MAJOR < 2
#else
#endif
    this->InitializeTypeDomain();
}

#if ARP_ABI_VERSION_MAJOR < 2
void TriggerRetainLibrary::Main(AppDomain& appDomain)
{
    SingletonBase::CreateInstance(appDomain);
}
#else
TriggerRetainLibrary& TriggerRetainLibrary::GetInstance()
{
    static TriggerRetainLibrary instance;
    return instance;
}
#endif


#if ARP_ABI_VERSION_MAJOR < 2
extern "C" ARP_CXX_SYMBOL_EXPORT ILibrary& ArpDynamicLibraryMain(AppDomain& appDomain)
{
    TriggerRetainLibrary::Main(appDomain);
    return  TriggerRetainLibrary::GetInstance();
}
} // end of namespace TriggerRetain
#else
} // end of namespace TriggerRetain
extern "C" ARP_EXPORT Arp::Base::Acf::Commons::ILibrary& TriggerRetain_MainEntry()
{
    return  TriggerRetain::TriggerRetainLibrary::GetInstance();
}
#endif


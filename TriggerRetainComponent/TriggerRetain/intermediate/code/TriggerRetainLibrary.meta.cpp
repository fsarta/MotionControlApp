#include "Arp/System/Core/Arp.h"
#include "Arp/Plc/Commons/Meta/TypeSystem/TypeSystem.h"
#include "TriggerRetainLibrary.hpp"

namespace TriggerRetain
{

using namespace Arp::Plc::Commons::Meta;
    
    void TriggerRetainLibrary::InitializeTypeDomain()
    {
        this->typeDomain.AddTypeDefinitions
        (
            // Begin TypeDefinitions
            {
            }
            // End TypeDefinitions
        );
    }

} // end of namespace TriggerRetain


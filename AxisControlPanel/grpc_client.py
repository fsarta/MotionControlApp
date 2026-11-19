# -*- coding: utf-8 -*-
import grpc
import logging
import json
import os
import xml.etree.ElementTree as ET 

from pxc_grpc.Plc.Gds.IDataAccessService_pb2 import (
    IDataAccessServiceReadSingleRequest,
    IDataAccessServiceWriteSingleRequest
)
from pxc_grpc.Plc.Gds.IDataAccessService_pb2_grpc import IDataAccessServiceStub

try:
    from pxc_grpc.Plc.Gds.IDataAccessService_pb2 import IDataAccessServiceGetPortListRequest
    HAS_GET_PORT_LIST = True
except ImportError:
    HAS_GET_PORT_LIST = False

logger = logging.getLogger(__name__)

class GrpcClient:
    def __init__(self, grpc_address):
        self.channel = grpc.insecure_channel(grpc_address)
        self.stub = IDataAccessServiceStub(self.channel)
        self.known_variables = set()
        
        # Dizionario per memorizzare quali variabili sono ENUM
        # Formato: {'Arp.Plc.Eclr/Main1.myENUM': True}
        self.enum_variables = {}
    
    def mark_as_enum(self, port_name):
        """Marca una variabile come ENUM"""
        self.enum_variables[port_name] = True
        logger.info(f"Variable {port_name} marked as ENUM")
    
    def is_enum(self, port_name):
        """Verifica se variabile ENUM"""
        return self.enum_variables.get(port_name, False)
    
    def get_all_variables(self, pattern="*"):
        if HAS_GET_PORT_LIST:
            try:
                request = IDataAccessServiceGetPortListRequest()
                if pattern and pattern != "*":
                    request.portNamePattern = pattern
                
                response = self.stub.GetPortList(request)
                
                variables = []
                if hasattr(response, '_ReturnValue') and response._ReturnValue:
                    for port_info in response._ReturnValue:
                        port_name = port_info.PortName if hasattr(port_info, 'PortName') else str(port_info)
                        variables.append(port_name)
                        self.known_variables.add(port_name)
                
                logger.info(f"Discovered {len(variables)} variables with pattern '{pattern}'")
                return variables
                
            except Exception as e:
                logger.error(f"Error using GetPortList: {e}")
                return self._get_manual_variable_list(pattern)
        else:
            logger.info("Using manual variable list (GetPortList not available)")
            return self._get_manual_variable_list(pattern)
    
    def _get_manual_variable_list(self, pattern="Arp.Plc.Eclr/Main1.*"):
        default_variables = [
            'Arp.Plc.Eclr/Main1.myBOOL',
            'Arp.Plc.Eclr/Main1.mySINT',
            'Arp.Plc.Eclr/Main1.myINT',
            'Arp.Plc.Eclr/Main1.myDINT',
            'Arp.Plc.Eclr/Main1.myLINT',
            'Arp.Plc.Eclr/Main1.myUSINT',
            'Arp.Plc.Eclr/Main1.myUINT',
            'Arp.Plc.Eclr/Main1.myUDINT',
            'Arp.Plc.Eclr/Main1.myULINT',
            'Arp.Plc.Eclr/Main1.myREAL',
            'Arp.Plc.Eclr/Main1.myLREAL',
            'Arp.Plc.Eclr/Main1.myTIME',
            'Arp.Plc.Eclr/Main1.myLTIME',
            'Arp.Plc.Eclr/Main1.myBYTE',
            'Arp.Plc.Eclr/Main1.myWORD',
            'Arp.Plc.Eclr/Main1.myDWORD',
            'Arp.Plc.Eclr/Main1.myLWORD',
            'Arp.Plc.Eclr/Main1.mySTRING',
            'Arp.Plc.Eclr/Main1.myCHAR',
            'Arp.Plc.Eclr/Main1.myWSTRING',
            'Arp.Plc.Eclr/Main1.myENUM',
            'Arp.Plc.Eclr/Main1.myARRAY',
            'Arp.Plc.Eclr/Main1.mySTRUCT',
        ]
        
        all_vars = list(self.known_variables.union(set(default_variables)))
        
        if pattern and pattern != "*":
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                filtered = [v for v in all_vars if v.startswith(prefix)]
            else:
                filtered = [v for v in all_vars if v == pattern]
            return filtered
        
        return all_vars
    
    def add_known_variable(self, port_name):
        self.known_variables.add(port_name)
    
    def set_manual_variables(self, variable_list):
        self.known_variables = set(variable_list)
        logger.info(f"Manual variable list set: {len(variable_list)} variables")
    
    def get_variables_info(self, pattern="Arp.Plc.Eclr/Main1.*"):
        try:
            variables = self.get_all_variables(pattern)
            
            detailed_vars = []
            for var_name in variables:
                try:
                    result = self.read_single(var_name)
                    if result.get('success', True): 
                        # Detect if it's an ENUM based on name
                        type_name = self._get_type_name(result.get('type_code', 0))
                        if 'enum' in var_name.lower():
                            type_name = 'ENUM'
                            self.mark_as_enum(var_name)
                        
                        detailed_vars.append({
                            'name': var_name,
                            'type_code': result.get('type_code', 0),
                            'current_value': result.get('value', None),
                            'type_name': type_name,
                            'writable': True,
                            'help_text': self._get_type_help_for_name(var_name, result.get('type_code', 0))
                        })
                except Exception as e:
                    logger.debug(f"Skipping variable {var_name}: {e}")
            
            logger.info(f"Retrieved detailed info for {len(detailed_vars)} variables")
            return detailed_vars
            
        except Exception as e:
            logger.error(f"Error getting detailed variables: {e}")
            return []
    
    def _get_type_name(self, type_code):
        """TypeCode mapping"""
        type_map = {
            1: 'VOID',
            2: 'BOOL',
            3: 'CHAR',
            4: 'SINT', 5: 'USINT',
            6: 'INT', 7: 'UINT',
            8: 'DINT', 9: 'UDINT',
            10: 'LINT', 11: 'ULINT',
            12: 'REAL', 13: 'LREAL',
            14: 'TIME', 15: 'LTIME',
            18: 'STRUCT',
            19: 'STRING',
            20: 'ARRAY',
            30: 'WSTRING',
            37: 'ENUM',
        }
        return type_map.get(type_code, f'UNKNOWN({type_code})')
    
    def _get_type_help(self, type_code):
        """Help text for type code"""
        help_map = {
            2: 'TRUE/FALSE or 1/0',
            3: 'Single character',
            4: 'Integer -128 to 127',
            5: 'Integer 0 to 255',
            6: 'Integer -32768 to 32767',
            7: 'Integer 0 to 65535',
            8: 'Integer -2147483648 to 2147483647',
            9: 'Integer 0 to 4294967295',
            10: '64-bit signed integer',
            11: '64-bit unsigned integer',
            12: '32-bit decimal',
            13: '64-bit decimal',
            14: 'Time in milliseconds',
            15: 'Time in nanoseconds',
            19: 'Text string',
            30: 'Unicode string',
            37: 'Enum value (0,1,2,...)',
        }
        return help_map.get(type_code, 'Value')
    
    def _get_type_help_for_name(self, var_name, type_code):
        """Help text based on variable name (detect ENUM)"""
        if 'enum' in var_name.lower():
            return 'Enum value (0,1,2,...)'
        return self._get_type_help(type_code)
    
    def read_single(self, port_name):
        try:
            self.known_variables.add(port_name)
            request = IDataAccessServiceReadSingleRequest()
            request.portName = port_name
            response = self.stub.ReadSingle(request)
            return self._parse_value(response._ReturnValue.Value, port_name)
        except Exception as e:
            logger.error(f"Error reading {port_name}: {e}")
            return {'success': False, 'error': str(e)}
    
    def read_multiple(self, port_names):
        results = []
        for port_name in port_names:
            result = self.read_single(port_name)
            result['port_name'] = port_name
            results.append(result)
        return results

    def _parse_value(self, value_obj, port_name=""):
        """Parse value - KEEPS WORKING TYPES, FULL DIAGNOSTICS"""
        tc = value_obj.TypeCode
        val = None

        # ===== WORKING TYPES - DON'T MODIFY =====
        if tc == 2:  # BOOL
            val = getattr(value_obj, 'BoolValue', None)
            if val is None:
                val = getattr(value_obj, 'BooleanValue', False)
        
        elif tc == 4:  # SINT
            val = getattr(value_obj, 'Int8Value', 0)
            
        elif tc == 5:  # USINT/BYTE
            val = getattr(value_obj, 'Uint8Value', 0)
            
        elif tc == 6:  # INT (or ENUM exposed as INT)
            val = getattr(value_obj, 'Int16Value', 0)
            # Check if this is actually an ENUM
            if 'enum' in port_name.lower():
                #logger.info(f"? Reading ENUM (exposed as INT TC6): {val}")
                self.mark_as_enum(port_name)
            
        elif tc == 7:  # UINT/WORD
            val = getattr(value_obj, 'Uint16Value', 0)
            
        elif tc == 8:  # DINT
            val = getattr(value_obj, 'Int32Value', 0)
            
        elif tc == 9:  # UDINT/DWORD
            val = getattr(value_obj, 'Uint32Value', 0)
            
        elif tc == 10:  # LINT
            val = getattr(value_obj, 'Int64Value', 0)
            
        elif tc == 11:  # ULINT/LWORD
            val = getattr(value_obj, 'UIntValue', None)
            if val is None:
                val = getattr(value_obj, 'Uint64Value', 0)
            #logger.info(f"? ULINT read: {val}")
            
        elif tc == 12:  # REAL
            val = getattr(value_obj, 'FloatValue', 0.0)
            
        elif tc == 13:  # LREAL
            val = getattr(value_obj, 'DoubleValue', 0.0)
        
        elif tc == 14:  # TIME
            val = getattr(value_obj, 'Int64Value', 0)
            
        elif tc == 15:  # LTIME
            val = getattr(value_obj, 'Uint64Value', 0)
            
        elif tc == 19:  # STRING
            val = getattr(value_obj, 'StringValue', "")
        
        elif tc == 3:  # CHAR
            char_code = getattr(value_obj, 'CharValue', 0)
            val = chr(char_code) if char_code != 0 else ''
            
        elif tc == 30:  # WSTRING
            val = getattr(value_obj, 'StringValue', "")
            
        elif tc == 37:  # ENUM (proper)
            val = getattr(value_obj, 'EnumValue', 0)
            #logger.info(f"? ENUM (proper TC37) read: {val}")
            
        elif tc == 20:  # ARRAY (proper)
            #logger.info("?? ARRAY (TC20) detected - attempting to parse ArrayValue")
            array_obj = getattr(value_obj, 'ArrayValue', None)
            if array_obj and hasattr(array_obj, 'ArrayElements'):
                val = []
                for element in array_obj.ArrayElements:
                    elem_result = self._parse_value(element)
                    val.append(elem_result['value'])
                #logger.info(f"? ARRAY parsed: {len(val)} elements = {val}")
            else:
                logger.warning("ARRAY: ArrayValue field not found or empty")
                val = []
            
        elif tc == 18:  # STRUCT (proper)
            #logger.info("?? STRUCT (TC18) detected - attempting to parse StructValue")
            struct_obj = getattr(value_obj, 'StructValue', None)
            if struct_obj and hasattr(struct_obj, 'StructElements'):
                val = {}
                for idx, element in enumerate(struct_obj.StructElements):
                    elem_result = self._parse_value(element)
                    val[f"field_{idx}"] = elem_result['value']
                #logger.info(f"? STRUCT parsed: {len(val)} fields = {val}")
            else:
                logger.warning("STRUCT: StructValue field not found or empty")
                val = {}
        
        elif tc == 1:  # VOID/Unknown
            # SILENZIATO IL FULL DIAGNOSTICS PER EVITARE SPAM NEI LOG
            # If it is VOID, try to see if there is any primitive value set
            if getattr(value_obj, 'BoolValue', False) is True: val = True
            elif getattr(value_obj, 'Int32Value', 0) != 0: val = getattr(value_obj, 'Int32Value')
            elif getattr(value_obj, 'FloatValue', 0.0) != 0.0: val = getattr(value_obj, 'FloatValue')
            else: val = 0 
            
        else:
            logger.warning(f"Unknown TypeCode {tc}")
            val = getattr(value_obj, 'Int32Value', 0)

        # Detect ENUM by name even if exposed as different type
        type_name = self._get_type_name(tc)
        if 'enum' in port_name.lower() and tc in [6, 8]:  # INT or DINT
            type_name = 'ENUM'
            #logger.info(f"?? Variable detected as ENUM by name: {port_name}")

        return {
            'type_code': tc,
            'type_name': type_name,
            'value': val,
            'success': True
        }

    def write_single(self, port_name, value, value_type=""):
        """Write - HANDLES ENUM AS INT"""
        try:
            self.known_variables.add(port_name)
            
            logger.info(f"Writing {port_name} = {value} (type hint: {value_type})")
            
            # 1. Read to get real TypeCode
            read_req = IDataAccessServiceReadSingleRequest()
            read_req.portName = port_name
            current_resp = self.stub.ReadSingle(read_req)
            current_val = current_resp._ReturnValue.Value
            real_tc = current_val.TypeCode
            
            #logger.info(f"Real TypeCode: {real_tc} ({self._get_type_name(real_tc)})")
            
            # === CRITICAL FIX FOR VOID (TC1) ===
            # If PLC returns VOID but we know the type, force correct TypeCode
            if real_tc == 1 and value_type:
                vt = value_type.upper()
                if 'BOOL' in vt: real_tc = 2
                elif 'SINT' in vt: real_tc = 4
                elif 'USINT' in vt: real_tc = 5
                elif 'INT' in vt: real_tc = 6
                elif 'UINT' in vt: real_tc = 7
                elif 'DINT' in vt: real_tc = 8
                elif 'UDINT' in vt: real_tc = 9
                elif 'REAL' in vt: real_tc = 12
                elif 'LREAL' in vt: real_tc = 13
                elif 'STRING' in vt: real_tc = 19
                logger.info(f"Target is VOID (TC1). Forcing type to {real_tc} based on hint: {value_type}")
            # ==================================

            # Detect if this is an ENUM
            is_enum = 'enum' in port_name.lower() or 'ENUM' in str(value_type).upper()
            if is_enum:
                self.mark_as_enum(port_name)
            
            # 2. Prepare write request
            write_req = IDataAccessServiceWriteSingleRequest()
            write_req.data.PortName = port_name
            write_req.data.Value.TypeCode = real_tc
            
            # 3. Set value
            
            # ===== WORKING TYPES - DON'T MODIFY =====
            if real_tc == 2:  # BOOL
                is_true = str(value).lower() in ['true', '1', 'yes', 't', 'on']
                try:
                    write_req.data.Value.BoolValue = is_true
                except:
                    write_req.data.Value.BooleanValue = is_true
            
            elif real_tc == 4:  # SINT
                ival = int(float(value))
                write_req.data.Value.Int8Value = ival
            
            elif real_tc == 5:  # USINT/BYTE
                ival = int(float(value))
                write_req.data.Value.Uint8Value = ival
            
            elif real_tc == 6:  # INT (or ENUM exposed as INT)
                ival = int(float(value))
                write_req.data.Value.Int16Value = ival
            
            elif real_tc == 7:  # UINT/WORD
                ival = int(float(value))
                write_req.data.Value.Uint16Value = ival
            
            elif real_tc == 8:  # DINT (or ENUM exposed as DINT)
                ival = int(float(value))
                write_req.data.Value.Int32Value = ival
            
            elif real_tc == 9:  # UDINT/DWORD
                ival = int(float(value))
                write_req.data.Value.Uint32Value = ival
            
            elif real_tc == 10:  # LINT
                ival = int(float(value))
                write_req.data.Value.Int64Value = ival
            
            elif real_tc == 11:  # ULINT/LWORD
                ival = int(float(value))
                write_req.data.Value.UIntValue = ival
            
            elif real_tc == 12:  # REAL
                fval = float(value)
                write_req.data.Value.FloatValue = fval
            
            elif real_tc == 13:  # LREAL
                fval = float(value)
                write_req.data.Value.DoubleValue = fval
            
            elif real_tc == 14:  # TIME
                time_ms = int(float(value))
                write_req.data.Value.Int64Value = time_ms
            
            elif real_tc == 15:  # LTIME
                time_ns = int(float(value))
                write_req.data.Value.Uint64Value = time_ns
            
            elif real_tc == 19:  # STRING
                write_req.data.Value.StringValue = str(value)
            
            elif real_tc == 3:  # CHAR
                char_str = str(value)
                char_code = ord(char_str[0]) if len(char_str) > 0 else 0
                write_req.data.Value.CharValue = char_code
            
            elif real_tc == 30:  # WSTRING
                write_req.data.Value.StringValue = str(value)
            
            elif real_tc == 37:  # ENUM (proper)
                ival = int(float(value))
                write_req.data.Value.EnumValue = ival
            
            elif real_tc == 20:  # ARRAY
                logger.error("? ARRAY write: TypeCode 20 detected but write not fully supported")
                raise ValueError("ARRAY write not supported - write individual elements instead")
            
            elif real_tc == 18:  # STRUCT
                logger.error("? STRUCT write: TypeCode 18 detected but write not fully supported")
                raise ValueError("STRUCT write not supported - write individual fields instead")
            
            elif real_tc == 1:  # VOID
                # If we get here, it means value_type was missing or override failed
                logger.error(f"? Cannot write to VOID type (TC1) for {port_name}")
                raise ValueError("Cannot write to VOID type")
            
            else:
                logger.warning(f"Unknown TypeCode {real_tc}, trying Int32Value")
                write_req.data.Value.Int32Value = int(float(value))
            
            # 4. Send
            response = self.stub.WriteSingle(write_req)
            # logger.info(f"? Successfully wrote {port_name}")
            return True
            
        except ValueError as ve:
            logger.error(f"? Value error: {ve}")
            return False
        except Exception as e:
            logger.error(f"? Write error: {e}", exc_info=True)
            return False
    
    def write_multiple(self, variables):
        """Write multiple variables"""
        success = True
        for var in variables:
            # Pass 'type' if present in dictionary to help VOID fix
            type_hint = var.get('type', '')
            result = self.write_single(var['port_name'], var['value'], type_hint)
            if not result:
                success = False
        return success

    def discover_axes(self):
        """
        Reads axis.xml from iCube Engineer and returns the list of GDS axes with number and node.
        """
        logger.info("Auto-Discovery: Reading axis.xml...")
        xml_path = "/opt/plcnext/projects/PCWE/Plc/MC/startup/axis.xml"
        
        found_axes = []
        if not os.path.exists(xml_path):
            logger.error("axis.xml not found")
            return []

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            axis_list = root.find("axisList")
            
            if axis_list is not None:
                prefix = "Arp.Plc.Eclr/"
                for axis in axis_list.findall("axis"):
                    name_elem = axis.find("name")
                    number_elem = axis.find("number")
                    node_elem = axis.find("node")
                    
                    if name_elem is not None and name_elem.text:
                        name = name_elem.text
                        gds_name = f"{prefix}{name}"
                        
                        number = int(number_elem.text) if (number_elem is not None and number_elem.text) else 0
                        node = int(node_elem.text) if (node_elem is not None and node_elem.text) else 0
                        
                        found_axes.append({
                            "name": gds_name, 
                            "simple_name": name,
                            "number": number,
                            "node": node
                        })
                        logger.info(f"Discovered from XML: {gds_name} (Num:{number}, Node:{node})")
            
            return found_axes
        except Exception as e:
            logger.error(f"Discovery error: {e}")
            return []

    def close(self):
        if hasattr(self, 'channel'): 
            self.channel.close()
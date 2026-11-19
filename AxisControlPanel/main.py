#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLCopen Part 1 Motion Control Web Interface
Complete implementation with all function blocks, logging, and optimization
Enhanced with external JSON configuration files for function blocks and error codes
"""
import sys
import os
import shutil
import subprocess
import glob

# Replace with the actual {name} you used when creating the parallel project 
APP_PROJECT_NAME = "AxisControlPanel" 
APP_ID = "20000000000004"

# Absolute path to the template bundled with your Python app
# Assumes 'icube_project_template' is in the same folder as main.py
APP_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icube_project_template')
APP_TRIGGER_RETAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'TriggerRetain')

# Cambio progetto robusto: reboot di sistema (non RestartDevice), sync, verifica,
# stato su disco per il watchdog sull'host. Vedi docs/switch_guide.md
import project_switch

project_switch.APP_PROJECT_NAME = APP_PROJECT_NAME
project_switch.PROJECTS_DIR = '/opt/plcnext/projects'

def run_bash(cmd):
    """Executes a bash command and logs the output."""
    try:
        logger.info(f"Executing: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, executable='/bin/bash')
        if result.stdout:
            logger.info(f"Output: {result.stdout}")
        if result.stderr:
            logger.warning(f"Stderr: {result.stderr}")
        result.check_returncode()
    except subprocess.CalledProcessError as e:
        logger.error(f"Error executing '{cmd}': {e}\nStdout: {e.stdout}\nStderr: {e.stderr}")
        return False
    return True

def setup_parallel_project():
    """utente -> template. La logica vera sta in project_switch (reboot reale)."""
    ok, msg = project_switch.activate_app_project(
        APP_PROJECT_NAME, APP_TEMPLATE_DIR, APP_TRIGGER_RETAIN_DIR
    )
    logger.info("activate_app_project -> ok=%s msg=%s", ok, msg)
    return ok


def teardown_parallel_project():
    """template -> utente. La logica vera sta in project_switch (reboot reale)."""
    ok, msg = project_switch.activate_user_project(APP_PROJECT_NAME)
    logger.info("activate_user_project -> ok=%s msg=%s", ok, msg)
    return ok


from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import logging
import signal
import time
import json
import threading
from datetime import datetime
from collections import deque
from threading import Lock
from grpc_client import GrpcClient

# Logging setup
log_dir = '/opt/plcnext/logs/grpc_webserver'
try:
    os.makedirs(log_dir, exist_ok=True)
except OSError:
    log_dir = '/tmp/grpc_webserver'
    os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(log_dir, 'motion_control.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Preserve key order from function_blocks.json (disable alphabetical sorting)
app.config['JSON_SORT_KEYS'] = False  # Flask < 2.2
try:
    app.json.sort_keys = False  # Flask >= 2.2
except AttributeError:
    pass
CORS(app)

grpc_client = None

MOTION_LOG_MAX = int(os.getenv('MOTION_LOG_MAX', 1000))
motion_logs = deque(maxlen=MOTION_LOG_MAX)
logs_lock = Lock()

READ_CACHE_TTL_SEC = float(os.getenv('READ_CACHE_TTL_SEC', 0.25))
read_cache = {}
read_cache_lock = Lock()

last_fb_outputs = {}
last_outputs_lock = Lock()

# ==================== PROJECT STATE ====================
# Tracks whether the app's parallel project is active on the PLC
# States: 'user_project', 'app_project', 'switching'
project_state = 'user_project'
project_state_lock = Lock()

def check_project_state():
    """Read the current symlink to determine the real project state."""
    global project_state
    try:
        current_target = os.path.realpath('/opt/plcnext/projects/current')
        with project_state_lock:
            if project_state == 'switching':
                return project_state  # Don't override while switching
            if APP_PROJECT_NAME in current_target:
                project_state = 'app_project'
            else:
                project_state = 'user_project'
            return project_state
    except Exception as e:
        logger.error(f"Error checking project state: {e}")
        return project_state

def _do_project_switch(action):
    """Esegue il cambio progetto. Il riavvio lo chiude project_switch: il
    container muore qui, quindi NON si aspetta piu' il ritorno del PLC in questo
    processo (era codice morto). Il completimento lo verifica host/switch-watchdog.sh
    leggendo /opt/plcnext/user/switch.state."""
    global project_state
    try:
        if action == 'activate':
            ok = setup_parallel_project()
        else:
            ok = teardown_parallel_project()
        with project_state_lock:
            project_state = 'app_project' if action == 'activate' else 'user_project'
        if not ok:
            logger.error("Project switch reported failure; see switch.state")
    except Exception as e:
        logger.error(f"Project switch failed: {e}", exc_info=True)
        with project_state_lock:
            check_project_state()


LOG_EVENT_OUTPUTS = {
    'Done',
    'Error',
    'CommandAborted',
    'InVelocity',
    'InTorque',
    'InGear',
    'InSync',
    'EndOfProfile'
}

# Load configuration files
PLCOPEN_FUNCTION_BLOCKS = {}
ERROR_CODES = {}

def load_json_configs():
    """Load function blocks and error codes from JSON files"""
    global PLCOPEN_FUNCTION_BLOCKS, ERROR_CODES
    
    # Try to load function_blocks.json
    fb_paths = [
        './json_data/function_blocks.json',
        '/opt/plcnext/config/function_blocks.json',
        '/opt/plcnext/user/config/function_blocks.json',
        './function_blocks.json',
        '/home/claude/function_blocks.json'
    ]
    
    for fb_path in fb_paths:
        if os.path.exists(fb_path):
            try:
                with open(fb_path, 'r', encoding='utf-8') as f:
                    PLCOPEN_FUNCTION_BLOCKS = json.load(f)
                logger.info(f"Loaded function blocks from: {fb_path}")
                logger.info(f"Loaded {len(PLCOPEN_FUNCTION_BLOCKS)} function blocks")
                break
            except Exception as e:
                logger.error(f"Failed to load function blocks from {fb_path}: {e}")
    
    if not PLCOPEN_FUNCTION_BLOCKS:
        logger.error("No function_blocks.json found! Motion control will not work properly.")
    
    # Try to load error_codes.json with multiple encodings
    error_paths = [
        './json_data/error_codes.json',
        '/opt/plcnext/config/error_codes.json',
        '/opt/plcnext/user/config/error_codes.json',
        './error_codes.json',
        '/home/claude/error_codes.json'
    ]
    
    for error_path in error_paths:
        if os.path.exists(error_path):
            # Try multiple encodings for Windows compatibility
            for encoding in ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']:
                try:
                    with open(error_path, 'r', encoding=encoding) as f:
                        ERROR_CODES = json.load(f)
                    logger.info(f"Loaded error codes from: {error_path} (encoding: {encoding})")
                    logger.info(f"Loaded {len(ERROR_CODES)} error codes")
                    break
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    if encoding == 'iso-8859-1':  # Last attempt
                        logger.error(f"Failed to load error codes from {error_path} with all encodings: {e}")
                    continue
            if ERROR_CODES:
                break
    
    if not ERROR_CODES:
        logger.warning("No error_codes.json found. Error descriptions will not be available.")
        ERROR_CODES = {"0": "No error"}

# ==================== GRPC CLIENT ====================

def init_grpc_client():
    """Initialize gRPC client"""
    global grpc_client
    try:
        # Default gRPC address for PLCnext
        grpc_address = os.getenv('GRPC_ADDRESS', 'unix:///run/plcnext/grpc.sock')
        grpc_client = GrpcClient(grpc_address)
        logger.info(f"gRPC client initialized successfully with address: {grpc_address}")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize gRPC client: {e}", exc_info=True)
        return False

def graceful_shutdown(signum, frame):
    """Handle shutdown signals.

    NON si fa nessun teardown qui: un SIGTERM arriva anche quando il WBM ferma
    l'app, quando il container viene aggiornato o quando il controller riavvia.
    Riportare il symlink a PCWE in quei casi e' sbagliato (e se il container
    viene ucciso a meta' switch si resta in uno stato indefinito). Il cambio
    progetto e' esplicito: solo /api/v1/project/switch.
    """
    logger.info(f"Received signal {signum}, exiting (no project teardown).")
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

# ==================== HELPER FUNCTIONS ====================

def add_motion_log(axis, fb_name, event, params, instance='default'):
    """Add an entry to the motion log"""
    with logs_lock:
        motion_logs.append({
            'timestamp': datetime.now().isoformat(),
            'axis': axis,
            'function_block': fb_name,
            'instance': instance,
            'event': event,
            'parameters': params
        })

def _resolve_fb_prefix(data, fb_name):
    """
    Resolve the full variable prefix for accessing FB instance ports.
    Returns: (axis_label, fb_instance, fb_prefix)
    """
    struct_var = data.get('struct_variable', '').strip()
    axis_var = data.get('axis_variable', '').strip()

    if struct_var:
        axis_label = struct_var
        fb_instance = 'default'
        fb_prefix = f"{struct_var}.{fb_name}"
    elif axis_var:
        axis_label = axis_var
        fb_instance = 'default'
        fb_prefix = f"{axis_var}.{fb_name}"
    else:
        return (None, None, None)

    return (axis_label, fb_instance, fb_prefix)

def _get_axis_feedback():
    """Read actual feedback values from the selected axis."""
    if grpc_client is None: return {}
    
    variables_to_read = [
        'Arp.Plc.Eclr/Main1.AxisController.MC_ReadActualPosition.Position',
        'Arp.Plc.Eclr/Main1.AxisController.MC_ReadActualVelocity.Velocity', 
        'Arp.Plc.Eclr/Main1.AxisController.MC_ReadActualTorque.Torque',
        'Arp.Plc.Eclr/Main1.AxisController.MC_ReadPositionError.PositionError'
    ]
    try:
        results = grpc_client.read_multiple(variables_to_read)
        feedback = {}
        for result in results:
            port_name = result.get('port_name', '')
            value = result.get('value')
            if 'ActualPosition.Position' in port_name:
                feedback['Actual Position'] = value
            elif 'ActualVelocity.Velocity' in port_name:
                feedback['Actual Velocity'] = value
            elif 'ActualTorque.Torque' in port_name:
                feedback['Actual Torque'] = value
            elif 'PositionError.PositionError' in port_name:
                feedback['Following Error'] = value
        return feedback
    except Exception as e:
        logger.error(f"Error reading feedback: {e}")
        return {}

def _log_output_events(axis_name_for_log, fb_name, fb_instance, outputs):
    """
    Log output events when they transition from False to True.
    This prevents spamming the log with repeated True values.
    """
    fb_key = (axis_name_for_log, fb_name, fb_instance)
    with last_outputs_lock:
        last_out = last_fb_outputs.get(fb_key, {})
        for output_name in LOG_EVENT_OUTPUTS:
            current_val = outputs.get(output_name)
            previous_val = last_out.get(output_name)
            if current_val is True and previous_val is not True:
                params = {'value': current_val}
                
                # Add feedback data
                params.update(_get_axis_feedback())
                
                # If Error, add ErrorID and desc
                if output_name == 'Error':
                    error_id = outputs.get('ErrorID', 0)
                    params['ErrorID'] = error_id
                    params['Description'] = ERROR_CODES.get(str(error_id), f"Unknown error code: {error_id}")

                add_motion_log(
                    axis_name_for_log,
                    fb_name,
                    output_name,
                    params,
                    fb_instance
                )
        last_fb_outputs[fb_key] = dict(outputs)

# ==================== HEALTH CHECK ====================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint that verifies actual gRPC connectivity"""
    if grpc_client is None:
        return jsonify({"status": "initializing", "grpc": False}), 503
    
    # Perform a real gRPC read to verify the connection is alive
    try:
        test_result = grpc_client.read_single("Arp.Plc.Eclr/Main1.AxisController.MC_Power.Status")
        grpc_ok = test_result.get('success', False) or test_result.get('type_code', 0) != 0
    except Exception:
        grpc_ok = False
    
    if grpc_ok:
        return jsonify({"status": "ok", "grpc": True}), 200
    else:
        return jsonify({"status": "degraded", "grpc": False}), 503

# ==================== GET FUNCTION BLOCKS ====================

@app.route('/api/plcopen/function_blocks', methods=['GET'])
def get_function_blocks():
    """Return the list of available function blocks and their definitions"""
    try:
        return jsonify({
            "function_blocks": PLCOPEN_FUNCTION_BLOCKS,
            "success": True
        }), 200
    except Exception as e:
        logger.error(f"Error getting function blocks: {e}")
        return jsonify({"error": str(e), "success": False}), 500

# ==================== ERROR CODE LOOKUP ====================

@app.route('/api/plcopen/error_code/<error_code>', methods=['GET'])
def get_error_description(error_code):
    """Get the description for a specific error code"""
    try:
        description = ERROR_CODES.get(str(error_code), f"Unknown error code: {error_code}")
        return jsonify({
            "error_code": error_code,
            "description": description,
            "success": True
        }), 200
    except Exception as e:
        logger.error(f"Error looking up error code: {e}")
        return jsonify({"error": str(e), "success": False}), 500

# ==================== EMERGENCY STOP ====================

@app.route('/api/plcopen/emergency_stop', methods=['POST'])
def emergency_stop():
    """Execute MC_Stop for emergency stop"""
    if grpc_client is None:
        init_grpc_client()

    try:
        data = request.get_json(silent=True) or {}
        
        # Resolve the FB prefix
        axis_label, fb_instance, fb_prefix = _resolve_fb_prefix(data, 'MC_Stop')
        if not fb_prefix:
            return jsonify({"error": "Missing struct_variable or axis_variable", "success": False}), 400

        # Get deceleration value from request or use default
        deceleration = data.get('deceleration', 10000.0)  # High deceleration for emergency
        jerk = data.get('jerk', 0.0)

        # Write MC_Stop inputs
        success = True
        
        # Set Deceleration FIRST
        result = grpc_client.write_single(f"{fb_prefix}.Deceleration", deceleration, "LREAL")
        if not result:
            success = False
            logger.error(f"Failed to set MC_Stop.Deceleration")
        
        # Set Jerk
        result = grpc_client.write_single(f"{fb_prefix}.Jerk", jerk, "LREAL")
        if not result:
            success = False
            logger.error(f"Failed to set MC_Stop.Jerk")
            
        # Set Execute to True AFTER parameters to trigger the stop with correct values
        result = grpc_client.write_single(f"{fb_prefix}.Execute", True, "BOOL")
        if not result:
            success = False
            logger.error(f"Failed to set MC_Stop.Execute")

        # Log the emergency stop
        params = {'deceleration': deceleration, 'jerk': jerk}
        params.update(_get_axis_feedback())
        axis_name_for_log = data.get('axis_name', axis_label)
        
        add_motion_log(
            axis_name_for_log,
            'MC_Stop',
            'EMERGENCY_STOP',
            params,
            fb_instance
        )

        return jsonify({
            "function_block": "MC_Stop",
            "axis": axis_label,
            "emergency_stop": True,
            "success": success
        }), 200 if success else 500

    except Exception as e:
        logger.error(f"Error executing emergency stop: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500

# ==================== READ FUNCTION BLOCK ====================

@app.route('/api/plcopen/read_fb', methods=['POST'])
def read_function_block():
    """Read all inputs, outputs, and inouts of a function block instance"""
    if grpc_client is None:
        init_grpc_client()

    try:
        data = request.get_json(silent=True) or {}
        fb_name = data.get('function_block')

        if not fb_name:
            return jsonify({"error": "Missing function_block", "success": False}), 400

        if fb_name not in PLCOPEN_FUNCTION_BLOCKS:
            return jsonify({"error": f"Unknown function block: {fb_name}", "success": False}), 404

        axis_label, fb_instance, fb_prefix = _resolve_fb_prefix(data, fb_name)
        if not fb_prefix:
            return jsonify({"error": "Missing struct_variable or axis_variable", "success": False}), 400

        fb_def = PLCOPEN_FUNCTION_BLOCKS[fb_name]

        cache_key = (fb_prefix, fb_name)
        now = time.time()
        with read_cache_lock:
            cached = read_cache.get(cache_key)

        if cached and (now - cached['timestamp']) < READ_CACHE_TTL_SEC:
            response_data = dict(cached['response'])
            response_data['from_cache'] = True
            return jsonify(response_data), 200

        # Build list of variables to read
        port_names = []
        var_mapping = {}

        # Inputs
        for input_name in fb_def.get('inputs', {}).keys():
            port_name = f"{fb_prefix}.{input_name}"
            port_names.append(port_name)
            var_mapping[port_name] = ('inputs', input_name)

        # Outputs
        for output_name in fb_def.get('outputs', {}).keys():
            port_name = f"{fb_prefix}.{output_name}"
            port_names.append(port_name)
            var_mapping[port_name] = ('outputs', output_name)

        # Read all variables
        results = grpc_client.read_multiple(port_names)

        # Organize results
        fb_state = {
            'inputs': {},
            'outputs': {},
            'inouts': {}
        }

        for input_name in fb_def.get('inputs', {}).keys():
            fb_state['inputs'][input_name] = None
        for output_name in fb_def.get('outputs', {}).keys():
            fb_state['outputs'][output_name] = None
        for inout_name in fb_def.get('inout', {}).keys():
            fb_state['inouts'][inout_name] = None

        for result in results:
            port_name = result.get('port_name')
            if port_name in var_mapping:
                category, name = var_mapping[port_name]
                fb_state[category][name] = result.get('value')

        axis_name_for_log = data.get('axis_name', axis_label)
        _log_output_events(axis_name_for_log, fb_name, fb_instance, fb_state['outputs'])

        # Get error description if there's an error
        error_id = fb_state['outputs'].get('ErrorID', 0)
        error_description = None
        if error_id and error_id != 0:
            error_description = ERROR_CODES.get(str(error_id), f"Unknown error code: {error_id}")

        response_data = {
            "function_block": fb_name,
            "instance": fb_instance,
            "axis": axis_label,
            "state": fb_state,
            "error_description": error_description,
            "success": True
        }

        with read_cache_lock:
            read_cache[cache_key] = {
                'timestamp': now,
                'response': response_data
            }

        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"Error reading function block: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/plcopen/execute_fb', methods=['POST'])
def execute_function_block():
    """Execute a function block by setting its inputs"""
    if grpc_client is None:
        init_grpc_client()

    try:
        data = request.get_json(silent=True) or {}
        fb_name = data.get('function_block')
        inputs = data.get('inputs', {}) or {}

        if not fb_name:
            return jsonify({"error": "Missing function_block", "success": False}), 400

        if fb_name not in PLCOPEN_FUNCTION_BLOCKS:
            return jsonify({"error": f"Unknown function block: {fb_name}", "success": False}), 404

        axis_label, fb_instance, fb_prefix = _resolve_fb_prefix(data, fb_name)
        if not fb_prefix:
            return jsonify({"error": "Missing struct_variable or axis_variable", "success": False}), 400

        fb_def = PLCOPEN_FUNCTION_BLOCKS[fb_name]

        # CRITICAL: Sort inputs so trigger signals (Execute, Enable)
        # are written LAST. This prevents the PLC from triggering the FB
        # before parameter values (Velocity, Position, etc.) are set.
        TRIGGER_INPUTS = {'Execute', 'Enable'}
        
        param_inputs = []   # Regular parameters (write first)
        trigger_inputs = [] # Trigger signals (write last)
        
        for input_name, input_value in inputs.items():
            if input_name in fb_def.get('inputs', {}):
                if input_value is None:
                    continue
                if input_name in TRIGGER_INPUTS:
                    trigger_inputs.append((input_name, input_value))
                else:
                    param_inputs.append((input_name, input_value))
        
        # Write parameters first, then triggers
        success = True
        for input_name, input_value in param_inputs + trigger_inputs:
            port_name = f"{fb_prefix}.{input_name}"
            input_type = fb_def['inputs'][input_name]['type']

            result = grpc_client.write_single(port_name, input_value, input_type)
            if not result:
                success = False
                logger.error(f"Failed to write {port_name} = {input_value}")

        # Log execution
        params = dict(inputs)
        params.update(_get_axis_feedback())
        axis_name_for_log = data.get('axis_name', axis_label)

        add_motion_log(axis_name_for_log, fb_name, 'Execute', params, fb_instance)

        return jsonify({
            "function_block": fb_name,
            "instance": fb_instance,
            "axis": axis_label,
            "inputs_written": inputs,
            "success": success
        }), 200 if success else 500

    except Exception as e:
        logger.error(f"Error executing function block: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/plcopen/logs', methods=['GET'])
def get_motion_logs():
    """Get motion control logs"""
    try:
        # Get query parameters
        limit = request.args.get('limit', 100, type=int)
        axis_filter = request.args.get('axis', None)
        fb_filter = request.args.get('function_block', None)
        event_filter = request.args.get('event', None)
        instance_filter = request.args.get('instance', None)

        with logs_lock:
            logs = list(motion_logs)

        # Apply filters
        if axis_filter:
            logs = [log for log in logs if log.get('axis') == axis_filter]
        if fb_filter:
            logs = [log for log in logs if log.get('function_block') == fb_filter]
        if event_filter:
            logs = [log for log in logs if log.get('event') == event_filter]
        if instance_filter:
            logs = [log for log in logs if log.get('instance') == instance_filter]

        # Limit results
        logs = logs[-limit:]

        return jsonify({
            "logs": logs,
            "count": len(logs),
            "success": True
        }), 200
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/plcopen/clear_logs', methods=['POST'])
def clear_motion_logs():
    """Clear motion control logs"""
    try:
        with logs_lock:
            motion_logs.clear()

        logger.info("Motion logs cleared")
        return jsonify({"success": True, "message": "Logs cleared"}), 200
    except Exception as e:
        logger.error(f"Error clearing logs: {e}")
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/v1/read', methods=['POST'])
def read_variable():
    """Read a single variable (generic endpoint)"""
    if grpc_client is None: 
        if not init_grpc_client():
            return jsonify({"error": "gRPC init failed", "success": False}), 500
    try:
        data = request.get_json(silent=True) or {}
        port_name = data.get('port_name')
        
        if not port_name:
            return jsonify({"error": "Missing port_name", "success": False}), 400
            
        result = grpc_client.read_single(port_name)
        
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Read error: {e}")
        return jsonify({"error": str(e), "success": False}), 500
    
@app.route('/api/v1/discover/axes', methods=['GET'])
def get_axes_list():
    if grpc_client is None: 
        if not init_grpc_client():
            return jsonify({"error": "gRPC init failed", "success": False}), 500
    try:
        # Call discovery function in client
        axes_list = grpc_client.discover_axes()
        
        return jsonify({
            "count": len(axes_list),
            "axes": axes_list,
            "success": True
        }), 200
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/v1/read/axis_controller_values', methods=['POST'])
def read_axis_controller_values():
    """
    Read axis values from AxisController using axis reference.
    The AxisController is a FB that reads/writes ST_AxisControl_Interface 
    using an AXIS_REF to select which axis to monitor.
    """
    if grpc_client is None: 
        if not init_grpc_client():
            return jsonify({"error": "gRPC init failed", "success": False}), 500
    
    try:
        data = request.get_json(silent=True) or {}
        axis_name = data.get('axis_name', '').strip()
        
        if not axis_name:
            return jsonify({"error": "Missing axis_name", "success": False}), 400
        
        # Define the variables to read from AxisController
        # These correspond to ST_AxisControl_Interface members
        variables_to_read = [
            'Arp.Plc.Eclr/Main1.AxisController.MC_ReadActualPosition.Position',
            'Arp.Plc.Eclr/Main1.AxisController.MC_ReadActualVelocity.Velocity', 
            'Arp.Plc.Eclr/Main1.AxisController.MC_ReadActualTorque.Torque',
            'Arp.Plc.Eclr/Main1.AxisController.MC_ReadPositionError.PositionError'
        ]
        
        # Read all variables at once
        results = grpc_client.read_multiple(variables_to_read)
        
        # Organize results
        values = {
            'position': None,
            'velocity': None,
            'torque': None,
            'following_error': None
        }
        
        for result in results:
            port_name = result.get('port_name', '')
            value = result.get('value')
            
            if 'ActualPosition.Position' in port_name:
                values['position'] = value
            elif 'ActualVelocity.Velocity' in port_name:
                values['velocity'] = value
            elif 'ActualTorque.Torque' in port_name:
                values['torque'] = value
            elif 'PositionError.PositionError' in port_name:
                values['following_error'] = value
        
        return jsonify({
            "axis_name": axis_name,
            "values": values,
            "success": True
        }), 200
        
    except Exception as e:
        logger.error(f"Error reading axis controller values: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/v1/copy/axis_ref', methods=['POST'])
def copy_axis_ref():
    """
    Copy the AXIS_REF structure from the selected axis to AXIS_CONTROLLER.
    This allows the AxisController FB to work with the selected axis.
    
    The AXIS_REF type is a complex STRUCT. However, in the parallel project,
    these variables don't exist. So instead of reading from the source axis PLC variable,
    we read from axis.xml and write only the critical AxisNum field to AXIS_CONTROLLER.
    """
    if grpc_client is None:
        if not init_grpc_client():
            return jsonify({"error": "gRPC init failed", "success": False}), 500
    
    try:
        data = request.get_json(silent=True) or {}
        source_axis = data.get('source_axis', '').strip() or data.get('axis_name', '').strip()
        target_axis = data.get('target_axis', '').strip() or "Arp.Plc.Eclr/AXIS_CONTROLLER"
        
        if not source_axis:
            return jsonify({"error": "Missing axis_name", "success": False}), 400
                
        logger.info(f"Setting AXIS_REF for {source_axis} to {target_axis} from axis.xml")
        
        # Get axis info from axis.xml
        axes = grpc_client.discover_axes()
        axis_info = None
        for ax in axes:
            if ax['name'] == source_axis or ax['simple_name'] == source_axis:
                axis_info = ax
                break
                
        if not axis_info:
            logger.error(f"Axis {source_axis} not found in axis.xml")
            return jsonify({
                "source_axis": source_axis,
                "target_axis": target_axis,
                "success": False,
                "error": f"Axis {source_axis} not found in axis.xml"
            }), 404
            
        axis_num = axis_info.get('number', 0)
        
        # Write AxisNum to AXIS_CONTROLLER
        target_var_num = f"{target_axis}.AxisNum"
        write_success_num = grpc_client.write_single(target_var_num, axis_num, "UINT")
        
        # Write DriverType = 1 to AXIS_CONTROLLER
        target_var_driver = f"{target_axis}.DriverType"
        write_success_driver = grpc_client.write_single(target_var_driver, 1, "INT")
        
        if write_success_num and write_success_driver:
            logger.info(f"Successfully configured AXIS_CONTROLLER with AxisNum={axis_num}, DriverType=1 for {source_axis}")
            return jsonify({
                "source_axis": source_axis,
                "target_axis": target_axis,
                "fields_copied": 2,
                "success": True,
                "message": f"AXIS_CONTROLLER configured successfully for AxisNum={axis_num}, DriverType=1"
            }), 200
        else:
            logger.error(f"Failed to write AxisNum or DriverType to {target_axis}")
            return jsonify({
                "source_axis": source_axis,
                "target_axis": target_axis,
                "fields_copied": (1 if write_success_num else 0) + (1 if write_success_driver else 0),
                "success": False,
                "error": "Failed to write AxisNum to PLC"
            }), 500
            
    except Exception as e:
        logger.error(f"Error configuring AXIS_REF: {e}", exc_info=True)
        return jsonify({"error": str(e), "success": False}), 500
# ==================== PROJECT SWITCH ENDPOINTS ====================

@app.route('/api/v1/project/status', methods=['GET'])
def get_project_status():
    """Return the current project state (user_project / app_project / switching)"""
    try:
        with project_state_lock:
            current_state = project_state
        
        # If not switching, verify against real symlink
        if current_state != 'switching':
            current_state = check_project_state()
        
        return jsonify({
            "state": current_state,
            "success": True
        }), 200
    except Exception as e:
        logger.error(f"Error getting project status: {e}")
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/api/v1/project/switch', methods=['POST'])
def switch_project():
    """Toggle the parallel project ON or OFF. Runs in a background thread."""
    global project_state
    
    try:
        with project_state_lock:
            if project_state == 'switching':
                return jsonify({
                    "error": "A switch operation is already in progress",
                    "state": "switching",
                    "success": False
                }), 409
            
            # Determine action based on current state
            if project_state == 'app_project':
                action = 'deactivate'
            else:
                action = 'activate'
            
            project_state = 'switching'
        
        logger.info(f"Project switch requested: {action}")
        
        # Run the switch in a background thread
        switch_thread = threading.Thread(
            target=_do_project_switch,
            args=(action,),
            daemon=True
        )
        switch_thread.start()
        
        return jsonify({
            "action": action,
            "state": "switching",
            "success": True
        }), 200
        
    except Exception as e:
        logger.error(f"Error during project switch: {e}")
        with project_state_lock:
            project_state = 'user_project'  # Safe fallback
        return jsonify({"error": str(e), "success": False}), 500

# ==================== SERVE HTML ====================

@app.route('/')
def index():
    """Serve the main HTML interface"""
    return render_template('index.html')

# ==================== MAIN ====================

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Starting PLCopen Motion Control Webserver")
    logger.info("=" * 60)
    
    # 1. Check current project state (do NOT auto-switch anymore)
    check_project_state()
    logger.info(f"Initial project state: {project_state}")
    
    # 2. Componente ACF: registrazione UNICA e sempre presente.
    #    Prima veniva cancellato quando il progetto app era attivo: risultato,
    #    su questo controller il componente non esisteva mai (backups/ vuota,
    #    BackupAction.txt "restore" mai consumato -> retain non conservati).
    if project_switch.install_acf_component(
            APP_TRIGGER_RETAIN_DIR,
            project_root=os.path.realpath('/opt/plcnext/projects/current')):
        logger.info("Componente ACF TriggerRetain registrato in /opt/plcnext/projects/Default/")
    else:
        logger.error("Registrazione componente ACF FALLITA: retain non disponibile")

    # 2. Load JSON configuration files
    load_json_configs()

    if not init_grpc_client():
        logger.error("Failed to initialize gRPC client")

    port = int(os.getenv('WEBSERVER_PORT', 8080))
    logger.info(f"Starting HTTP server on port {port}")

    app.run(host='0.0.0.0', port=port, debug=False)
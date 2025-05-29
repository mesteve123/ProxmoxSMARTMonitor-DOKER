# app.py
from flask import Flask, request, render_template
import paramiko
import re
import io
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# The SSH command to execute on the Proxmox server
SMART_COMMAND = """
#!/bin/bash
for disk in $(lsblk -d -n -o NAME | grep -Ev '^(loop|nbd|ram|fd)'); do
    echo "== SMART info for /dev/$disk =="
    smartctl -a /dev/$disk
    echo ""
done
"""

def parse_smart_output(output):
    """
    Parses the raw smartctl output to extract relevant disk information.
    """
    disks_info = []
    # Split the output into sections for each disk
    # The regex captures the device name and then the subsequent block of SMART data
    disk_sections = re.split(r'== SMART info for /dev/(.*?)\s*==\s*\n', output, flags=re.DOTALL)

    # The first element will be empty or pre-amble before the first disk section, so we skip it.
    # We iterate by stepping 2 because each match gives us (device_name, smart_data_block)
    for i in range(1, len(disk_sections), 2):
        device_name = disk_sections[i].strip()
        smart_data_block = disk_sections[i+1] # This is the full SMART output for the current disk

        current_disk_info = {
            "device_name": device_name,
            "model": "N/A",
            "capacity": "N/A",
            "type": "N/A",
            "power_cycles": "N/A",
            "power_on_hours": "N/A",
            "overall_health": "UNKNOWN",
            "smart_status": "UNKNOWN", # Initial status, will be refined
            "full_smart_output": smart_data_block.strip()
        }

        # Extract specific information using regex patterns
        model_match = re.search(r"Device Model:\s*(.*)", smart_data_block)
        if model_match:
            current_disk_info["model"] = model_match.group(1).strip()

        capacity_match = re.search(r"User Capacity:\s*(.*)", smart_data_block)
        if capacity_match:
            current_disk_info["capacity"] = capacity_match.group(1).strip()

        rotation_match = re.search(r"Rotation Rate:\s*(.*)", smart_data_block)
        if rotation_match:
            if "Solid State Device" in rotation_match.group(1):
                current_disk_info["type"] = "SSD"
            else:
                current_disk_info["type"] = "HDD"

        health_match = re.search(r"SMART overall-health self-assessment test result:\s*(.*)", smart_data_block)
        if health_match:
            current_disk_info["overall_health"] = health_match.group(1).strip()

        # Parse SMART Attributes for Power_On_Hours, Power_Cycle_Count, and other health indicators
        # This section tries to find the table of attributes and then parses each line
        attributes_section_match = re.search(
            r"ID# ATTRIBUTE_NAME.*?RAW_VALUE\s*\n(.*?)(?=\nSMART Error Log Version:|\nSMART Self-test log structure revision number:|$)",
            smart_data_block, re.DOTALL
        )

        if attributes_section_match:
            attributes_lines = attributes_section_match.group(1).strip().splitlines()
            for attr_line in attributes_lines:
                # Regex to extract attribute name and its raw value
                attr_match = re.search(r"^\s*\d+\s+([\w_]+)\s+.*?\s+(\S+)$", attr_line)
                if attr_match:
                    attribute_name = attr_match.group(1)
                    raw_value = attr_match.group(2)

                    if attribute_name == "Power_Cycle_Count":
                        current_disk_info["power_cycles"] = raw_value
                    elif attribute_name == "Power_On_Hours":
                        # Power_On_Hours can be in different formats (e.g., "579" or "17658h+45m+47.610s")
                        # Extract just the hours part if it's in the complex format
                        hours_match = re.search(r"(\d+)h", raw_value)
                        if hours_match:
                            current_disk_info["power_on_hours"] = hours_match.group(1) + " hours"
                        else:
                            current_disk_info["power_on_hours"] = raw_value + " hours"
                    elif attribute_name == "Remaining_Lifetime_Perc" and current_disk_info["type"] == "SSD":
                        try:
                            perc = int(raw_value)
                            if perc < 10:
                                current_disk_info["smart_status"] = "BAD"
                            elif perc < 20 and current_disk_info["smart_status"] != "BAD": # Don't downgrade from BAD
                                current_disk_info["smart_status"] = "WARNING"
                        except ValueError:
                            pass # Ignore if not a number
                    elif attribute_name in ["Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable"] and current_disk_info["type"] == "HDD":
                        try:
                            val = int(raw_value)
                            if val > 0 and current_disk_info["smart_status"] != "BAD": # Don't downgrade from BAD
                                current_disk_info["smart_status"] = "WARNING"
                        except ValueError:
                            pass

        # Determine final SMART status based on overall health and specific attributes
        if current_disk_info["overall_health"] == "PASSED":
            if current_disk_info["smart_status"] == "UNKNOWN":
                current_disk_info["smart_status"] = "OK"
        elif current_disk_info["overall_health"] == "FAILED":
            current_disk_info["smart_status"] = "BAD"

        # Check for general warnings in the entire block (e.g., "Warning! ATA error count...")
        if "Warning!" in smart_data_block and current_disk_info["smart_status"] != "BAD":
            current_disk_info["smart_status"] = "WARNING"

        disks_info.append(current_disk_info)

    return disks_info

@app.route("/", methods=["GET"])
def index():
    """Renders the main form page."""
    return render_template("index.html")

@app.route("/run_smartctl", methods=["POST"])
def run_smartctl():
    """
    Handles the form submission, connects to the server, runs smartctl,
    and displays the results.
    """
    ip = request.form["ip"]
    port = int(request.form["port"])
    username = request.form["username"]
    password = request.form["password"]

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy()) # Auto add host key for simplicity, consider more secure options in production

    error_message = None
    disks_info = []

    try:
        logging.info(f"Attempting to connect to {username}@{ip}:{port}")
        client.connect(hostname=ip, port=port, username=username, password=password, timeout=10)
        logging.info("SSH connection established successfully.")

        # Execute the SMART command
        logging.info("Executing SMART command...")
        stdin, stdout, stderr = client.exec_command(SMART_COMMAND)
        output = stdout.read().decode("utf-8")
        error_output = stderr.read().decode("utf-8")

        if error_output:
            logging.warning(f"Command stderr: {error_output}")
            # If smartctl is not found or permissions issues, it will be in stderr
            if "smartctl: command not found" in error_output:
                error_message = "Error: smartctl command not found on the remote server. Please ensure smartmontools is installed."
            else:
                error_message = f"Command execution error: {error_output}"
        else:
            logging.info("SMART command executed successfully. Parsing output...")
            disks_info = parse_smart_output(output)
            logging.info(f"Parsed {len(disks_info)} disks.")

    except paramiko.AuthenticationException:
        error_message = "Authentication failed. Please check your username and password."
        logging.error(f"Authentication failed for {username}@{ip}")
    except paramiko.SSHException as e:
        error_message = f"SSH connection error: {e}"
        logging.error(f"SSH error for {username}@{ip}: {e}")
    except paramiko.BadHostKeyException as e:
        error_message = f"Bad host key: {e}. The host key for this server has changed or is incorrect."
        logging.error(f"Bad host key for {ip}: {e}")
    except Exception as e:
        error_message = f"An unexpected error occurred: {e}"
        logging.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        if client:
            client.close()
            logging.info("SSH client closed.")

    return render_template("index.html", disks_info=disks_info, error_message=error_message)

if __name__ == "__main__":
    # In a production environment, you would typically run this with a WSGI server like Gunicorn.
    # For local development, this is sufficient.
    app.run(host="0.0.0.0", port=5000, debug=True)
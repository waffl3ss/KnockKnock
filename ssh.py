import subprocess
import socket
import time
import random
import os
import httpx
import logging

class SSHProxyManager:
    """
    Manages multiple SSH tunnels as SOCKS5 proxies using the system's ssh client.

    This class programmatically starts and stops `ssh -D` processes in the
    background. It robustly verifies that each proxy is active by polling
    its local port before considering it ready. It is designed to be used
    as a context manager for reliable setup and teardown.
    """
    def __init__(self, ssh_configs):
        """
        Initializes the manager with a list of SSH configurations.
        Each config should be a dict with keys: 'ip', 'username', 'private_key_path'.
        """
        self.ssh_configs = ssh_configs
        self.processes = []
        self.proxies = []

    def _find_free_port(self):
        """Atomically finds and returns a free local TCP port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    def start_tunnels(self):
        """
        Starts one `ssh -D` process for each server configuration and verifies
        readiness by polling the local SOCKS port.
        """
        for config in self.ssh_configs:
            local_port = self._find_free_port()
            ssh_command = [
                'ssh',
                '-N',  # Do not execute a remote command.
                '-q',  # Quiet mode, suppresses most messages.
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-o', 'ExitOnForwardFailure=yes',
                '-o', 'ServerAliveInterval=60',  # Keeps the connection alive.
                '-o', 'ConnectTimeout=30',       # Timeout for the initial connection.
                '-i', os.path.expanduser(config['private_key_path']),
                '-D', str(local_port),
                f"{config['username']}@{config['ip']}"
            ]

            try:
                # Start the ssh process in the background, capturing stderr for diagnostics.
                process = subprocess.Popen(ssh_command, stderr=subprocess.PIPE)

                # Robustly poll for the SOCKS port to become active.
                timeout_seconds = 15
                start_time = time.monotonic()
                port_is_open = False
                while time.monotonic() - start_time < timeout_seconds:
                    # Check if the process terminated prematurely with an error.
                    if process.poll() is not None:
                        stderr = process.stderr.read().decode(errors='ignore')
                        raise RuntimeError(f"SSH process terminated unexpectedly. Error: {stderr.strip()}")

                    # Attempt to connect to the local port.
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        if sock.connect_ex(('127.0.0.1', local_port)) == 0:
                            port_is_open = True
                            break  # Success! The proxy is ready.
                    
                    time.sleep(0.1)  # Avoid busy-waiting.

                if not port_is_open:
                    process.terminate()
                    process.wait()
                    stderr = process.stderr.read().decode(errors='ignore')
                    raise TimeoutError(
                        f"Timed out waiting for SOCKS proxy on port {local_port}. "
                        f"SSH process stderr: {stderr.strip()}"
                    )

                # If we get here, the tunnel is successfully established.
                self.processes.append(process)
                proxy_url = f'socks5h://127.0.0.1:{local_port}'
                self.proxies.append(proxy_url)
                logging.info(f"Started SOCKS5 proxy via {config['ip']} on 127.0.0.1:{local_port} (PID: {process.pid})")

            except (FileNotFoundError, RuntimeError, TimeoutError, Exception) as e:
                logging.info(f"Failed to start tunnel for {config['ip']}: {e}")

    def test_tunnels(self):
        for i, proxy in enumerate(self.proxies):
            with httpx.Client(proxy=proxy) as client:
                try:
                    response = client.get(
                        url="http://httpbin.org/ip",
                        timeout=30,
                        follow_redirects=True
                    )
                    responseJson = response.json()
                    ipReturned = responseJson["origin"]
                    ipExpected = self.ssh_configs[i]['ip']
                    if ipReturned == ipExpected:
                        logging.info(f"{proxy} test successful")
                    else:
                        logging.error(f"{proxy} test fail")
                except Exception as e:
                    logging.error(e)
    
    def stop_tunnels(self):
        """
        Gracefully terminates all managed ssh processes.
        """
        if not self.processes:
            return
            
        logging.info("\nStopping all SSH proxy processes...")
        for process in self.processes:
            try:
                if process.poll() is None: # Check if process is still running
                    process.terminate()  # Sends SIGTERM for graceful shutdown.
                    process.wait(timeout=5)
                    logging.debug(f"Terminated process with PID: {process.pid}")
            except subprocess.TimeoutExpired:
                logging.debug(f"Process {process.pid} did not terminate gracefully, killing.")
                process.kill()  # Sends SIGKILL as a last resort.
            except Exception as e:
                logging.debug(f"Error stopping process {process.pid}: {e}")
                
        self.processes = []
        self.proxies = []
        logging.info("All SSH proxy processes have been stopped.")

    def get_random_proxy(self):
        """Returns the URL of a randomly chosen active proxy."""
        if not self.proxies:
            raise Exception("No active proxies available.")
        return random.choice(self.proxies)

    def __enter__(self):
        self.start_tunnels()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_tunnels()
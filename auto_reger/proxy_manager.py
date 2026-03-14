import re
import time
from typing import Dict


class DecodoProxyManager:
    """
    Manages proxy rotation for Sticky Sessions, parsing session duration from the username.

    This class handles usernames that may contain session duration specifications (e.g.,
    'user-xyz-sessionduration-30'). It cycles through session IDs from 1 up to a
    specified maximum. The session ID counter is reset only when the allocated
    session duration expires. If the session limit is reached before the time is up,
    it waits for the remaining time before resetting.

    Args:
        raw_username (str): The proxy username, which may include session details.
        password (str): The proxy password.
        ip (str): The proxy server IP address.
        port (Union[str, int]): The proxy server port.
        max_sessions (int, optional): The maximum number of session IDs to rotate through.
                                      Defaults to 10.
    """

    def __init__(self, raw_username: str, password: str, ip: str, port, max_sessions: int = 10):
        self.password = password
        self.ip = ip
        self.port = str(port)
        self.max_sessions = max_sessions

        # Parse session duration from username
        duration_match = re.search(r'sessionduration-(\d+)', raw_username)
        if duration_match:
            self.duration_mins = int(duration_match.group(1))
            self._had_sessionduration = True
        else:
            self.duration_mins = 10
            self._had_sessionduration = False

        self.session_duration_sec = self.duration_mins * 60

        # Clean the username to get the base part
        self.base_username = re.sub(r'-session.*', '', raw_username)

        # Initialize state
        self.session_id = 1
        self.cycle_start_time = time.time()

    def get_next_proxy_env(self) -> Dict[str, str]:
        """
        Calculates and returns the environment variables for the next proxy configuration.

        This method implements the core logic for session rotation:
        1. Checks if the session duration has expired and resets the cycle if it has.
        2. Checks if the session limit has been reached; if so, waits for the current
           cycle's time to complete before resetting.
        3. Constructs the appropriate username for the current session.
        4. Increments the session ID for the next call.

        Returns:
            Dict[str, str]: A dictionary with proxy details ('PROXY_USER', 'PROXY_PASS',
                            'PROXY_IP', 'PROXY_PORT') for use as environment variables.
        """
        elapsed = time.time() - self.cycle_start_time

        # 1. Reset if the cycle time has elapsed
        if elapsed >= self.session_duration_sec:
            self.session_id = 1
            self.cycle_start_time = time.time()

        # 2. Handle hitting the session limit before the time is up
        elif self.session_id > self.max_sessions:
            wait_time = self.session_duration_sec - elapsed
            print(f"Session limit reached. Waiting for {wait_time:.2f} seconds...")
            time.sleep(wait_time + 5)  # Add 5s buffer for safety
            self.session_id = 1
            self.cycle_start_time = time.time()

        # Construct the username for the current session
        if self._had_sessionduration:
            current_username = (
                f"{self.base_username}-session-{self.session_id}"
                f"-sessionduration-{self.duration_mins}"
            )
        else:
            current_username = f"{self.base_username}-session-{self.session_id}"

        env_dict = {
            'PROXY_USER': current_username,
            'PROXY_PASS': self.password,
            'PROXY_IP': self.ip,
            'PROXY_PORT': self.port,
        }

        # Increment session ID for the next call
        self.session_id += 1

        return env_dict

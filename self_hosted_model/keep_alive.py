cat catalyst-config.json 
{
        "deployment": {
                "name": "lightning-recovery",
                "stack": "python_3_11",
                "type": "cron",
                "env_variables": {}
        },
        "execution": {
                "main": "main.py"
        }
}
(base) keyuarbhelonde@192 lightning-recovery % cat catalyst-inputs.json 
{
  "test_cron_input": {
    "test_param": "test_value"
  }
}
(base) keyuarbhelonde@192 lightning-recovery % cat main.py 
import io
import os
import time
import logging
import paramiko

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FULL_RUN_SECONDS = 210 * 60
CHECK_WINDOW_SECONDS = 9 * 60


def handler(cron_details, context):
    client = None

    try:
        username = os.environ["LIGHTNING_SSH_USERNAME"]
        private_key = os.environ["LIGHTNING_SSH_PRIVATE_KEY"]
        known_hosts = os.environ["LIGHTNING_SSH_KNOWN_HOSTS"]

        # Verify the SSH server's host key; never disable host verification.
        key_file = io.StringIO(private_key)
        pkey = None

        for key_type in (
            paramiko.Ed25519Key,
            paramiko.RSAKey,
            paramiko.ECDSAKey,
        ):
            key_file.seek(0)
            try:
                pkey = key_type.from_private_key(key_file)
                break
            except (paramiko.SSHException, ValueError):
                continue

        if pkey is None:
            raise ValueError("Unsupported or invalid SSH private key")

        known_hosts_path = "/tmp/lightning_known_hosts"
        with open(known_hosts_path, "w") as f:
            f.write(known_hosts.rstrip() + "\n")

        client = paramiko.SSHClient()
        client.load_host_keys(known_hosts_path)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        client.connect(
            hostname="ssh.lightning.ai",
            username=username,
            pkey=pkey,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
            look_for_keys=False,
            allow_agent=False,
        )

        # Each scheduled run checks SSH. In the first 9-minute window
        # of each 210-minute epoch, also execute the full setup script.
        now = int(time.time())
        run_full_setup = (now % FULL_RUN_SECONDS) < CHECK_WINDOW_SECONDS

        if run_full_setup:
            command = "bash -lc 'cd ~ && ./create_all_files.sh'"
            logger.info("SSH connected; running scheduled setup.")
        else:
            command = "printf 'LIGHTNING_SSH_OK\\n'"
            logger.info("SSH connected; recovery check completed.")

        stdin, stdout, stderr = client.exec_command(
            command, timeout=1200 if run_full_setup else 30
        )

        exit_code = stdout.channel.recv_exit_status()

        # Do not log stdout/stderr: the setup script may print secrets.
        if exit_code != 0:
            error = stderr.read().decode("utf-8", errors="replace")
            logger.error(
                "Remote command failed (exit=%s). Error output omitted "
                "to avoid leaking credentials.", exit_code
            )
            raise RuntimeError("Remote command failed")

        logger.info("Remote command completed successfully.")
        context.close_with_success()

    except Exception as exc:
        logger.error("Lightning recovery failed: %s", type(exc).__name__)
        context.close_with_failure()

    finally:
        if client:
            client.close()
(base) keyuarbhelonde@192 lightning-recovery % cat requirements.txt 
zcatalyst-sdk==1.4.0
paramiko==3.5.1
(base) keyuarbhelonde@192 lightning-recovery % 



xport LIGHTNING_SSH_USERNAME='s_01m4xsvdbpnn7z95skrmjr21q6'

export LIGHTNING_SSH_PRIVATE_KEY="$(cat ~/.ssh/lightning_rsa)"

export LIGHTNING_SSH_KNOWN_HOSTS='ssh.lightning.ai ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDCs840CVshlma61ItVseAz9d6P2k+snmjQI5pTROe/uhN0zGf0qypyGiRWk4Mv2shFeifTwHHQST+uNyIo1uyujrdhbQAvQ5DYxQzCIV3lxcn+E/5F/EK6Qx8lCbCkn0xutfTmWboZvf3VMHwTwbJyBXGdHBCIHm6Ais+tXaM4dtdjEUEFH0tS6fo1f/MBNsQtY/ulZVMVeFbofweBNkzHWdCgFG5YhZheU7Rl8ev2Gq2EykMe+LkLy6GLvAdGNWdahjsQGkMS0Mq9L/vr1gte8XRyaDB1h6q4HT9Due5I9EEwlu5UHLJnAQmj/mwEMNFq/839QwmzhAe8k/PqBJi/n/BTzkc1+mdIepVqMB+i5IWm3JK2WwlljTDlCFUygK63OKI9Gx0hcBKPPWdD6BH4hkN0auP3huJ07SQ/Jq3BES4U3OxRU/lsQ3oPo9d2frNLo5RmtbWBItt8OJIlVGb2VpmmIH/ksdlWq9VJaRYRRj4z/5ItjFI2kOfIrgQOfRE='
(base) keyuarbhelonde@192 lightning-recovery % for v in LIGHTNING_SSH_USERNAME LIGHTNING_SSH_PRIVATE_KEY LIGHTNING_SSH_KNOWN_HOSTS; do
  if [ -n "${!v}" ]; then echo "$v: SET"; else echo "$v: MISSING"; fi
done
zsh: event not found: v
(base) keyuarbhelonde@192 lightning-recovery % for v in LIGHTNING_SSH_USERNAME LIGHTNING_SSH_PRIVATE_KEY LIGHTNING_SSH_KNOWN_HOSTS; do
  if [ -n "${" ]; then echo "$v: SET"; else echo "$v: MISSING"; fi
done
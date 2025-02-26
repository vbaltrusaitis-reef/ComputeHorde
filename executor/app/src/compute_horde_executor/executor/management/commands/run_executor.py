import asyncio
import base64
import io
import logging
import pathlib
import shutil
import tempfile
import time
import zipfile

import httpx
import pydantic
from compute_horde.base_requests import BaseRequest
from compute_horde.em_protocol import executor_requests, miner_requests
from compute_horde.em_protocol.executor_requests import (
    GenericError,
    V0FailedRequest,
    V0FailedToPrepare,
    V0FinishedRequest,
    V0ReadyRequest,
)
from compute_horde.em_protocol.miner_requests import (
    BaseMinerRequest,
    V0InitialJobRequest,
    V0JobRequest,
    VolumeType,
)
from compute_horde.miner_client.base import AbstractMinerClient, UnsupportedMessageReceived
from django.conf import settings
from django.core.management.base import BaseCommand

from compute_horde_executor.executor.output_uploader import OutputUploader, OutputUploadFailed

logger = logging.getLogger(__name__)

temp_dir = pathlib.Path(tempfile.mkdtemp())
volume_mount_dir = temp_dir / 'volume'
output_volume_mount_dir = temp_dir / 'output'

CVE_2022_0492_TIMEOUT_SECONDS = 120
MAX_RESULT_SIZE_IN_RESPONSE = 1000
TRUNCATED_RESPONSE_PREFIX_LEN = 100
TRUNCATED_RESPONSE_SUFFIX_LEN = 100
INPUT_VOLUME_UNPACK_TIMEOUT_SECONDS = 300


class RunConfigManager:
    @classmethod
    def preset_to_docker_run_args(cls, preset: str) -> list[str]:
        if preset == 'none':
            return []
        elif preset == 'nvidia_all':
            return ['--runtime=nvidia', '--gpus', 'all']
        else:
            raise JobError(f"Invalid preset: {preset}")


class MinerClient(AbstractMinerClient):
    def __init__(self, loop: asyncio.AbstractEventLoop, miner_address: str, token: str):
        super().__init__(loop, '')
        self.miner_address = miner_address
        self.token = token
        self.job_uuid: str | None = None
        self.initial_msg = asyncio.Future()
        self.initial_msg_lock = asyncio.Lock()
        self.full_payload = asyncio.Future()
        self.full_payload_lock = asyncio.Lock()

    def miner_url(self) -> str:
        return f'{self.miner_address}/v0/executor_interface/{self.token}'

    def accepted_request_type(self) -> type[BaseRequest]:
        return BaseMinerRequest

    def incoming_generic_error_class(self):
        return miner_requests.GenericError

    def outgoing_generic_error_class(self):
        return executor_requests.GenericError

    async def handle_message(self, msg: BaseRequest):
        if isinstance(msg, V0InitialJobRequest):
            await self.handle_initial_job_request(msg)
        elif isinstance(msg, V0JobRequest):
            await self.handle_job_request(msg)
        else:
            raise UnsupportedMessageReceived(msg)

    async def handle_initial_job_request(self, msg: V0InitialJobRequest):
        async with self.initial_msg_lock:
            if self.initial_msg.done():
                msg = f'Received duplicate initial job request: first {self.job_uuid=} and then {msg.job_uuid=}'
                logger.error(msg)
                self.deferred_send_model(GenericError(details=msg))
                return
            self.job_uuid = msg.job_uuid
            logger.debug(f'Received initial job request: {msg.job_uuid=}')
            self.initial_msg.set_result(msg)

    async def handle_job_request(self, msg: V0JobRequest):
        async with self.full_payload_lock:
            if not self.initial_msg.done():
                msg = f'Received job request before an initial job request {msg.job_uuid=}'
                logger.error(msg)
                await self.deferred_send_model(GenericError(details=msg))
                return
            if self.full_payload.done():
                msg = (f'Received duplicate full job payload request: first '
                       f'{self.job_uuid=} and then {msg.job_uuid=}')
                logger.error(msg)
                await self.deferred_send_model(GenericError(details=msg))
                return
            logger.debug(f'Received full job payload request: {msg.job_uuid=}')
            self.full_payload.set_result(msg)

    async def send_ready(self):
        await self.send_model(V0ReadyRequest(job_uuid=self.job_uuid))

    async def send_finished(self, job_result: 'JobResult'):
        await self.send_model(V0FinishedRequest(
            job_uuid=self.job_uuid,
            docker_process_stdout=job_result.stdout,
            docker_process_stderr=job_result.stderr,
        ))

    async def send_failed(self, job_result: 'JobResult'):
        await self.send_model(V0FailedRequest(
            job_uuid=self.job_uuid,
            docker_process_exit_status=job_result.exit_status,
            timeout=job_result.timeout,
            docker_process_stdout=job_result.stdout,
            docker_process_stderr=job_result.stderr,
        ))

    async def send_generic_error(self, details: str):
        await self.send_model(GenericError(
            details=details,
        ))

    async def send_failed_to_prepare(self):
        await self.send_model(V0FailedToPrepare(
            job_uuid=self.job_uuid,
        ))


class JobResult(pydantic.BaseModel):
    success: bool
    exit_status: int | None
    timeout: bool
    stdout: str
    stderr: str


def truncate(v: str) -> str:
    if len(v) > MAX_RESULT_SIZE_IN_RESPONSE:
        return f'{v[:TRUNCATED_RESPONSE_PREFIX_LEN]} ... {v[-TRUNCATED_RESPONSE_SUFFIX_LEN:]}'
    else:
        return v


class JobError(Exception):
    def __init__(self, description: str):
        self.description = description


class JobRunner:
    def __init__(self, initial_job_request: V0InitialJobRequest):
        self.initial_job_request = initial_job_request

    async def prepare(self):
        volume_mount_dir.mkdir(exist_ok=True)
        output_volume_mount_dir.mkdir(exist_ok=True)

        process = await asyncio.create_subprocess_exec(
            'docker', 'pull', self.initial_job_request.base_docker_image_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            msg = (f'"docker pull {self.initial_job_request.base_docker_image_name}" '
                   f'(job_uuid={self.initial_job_request.job_uuid})'
                   f' failed with status={process.returncode}'
                   f' stdout="{stdout.decode()}"\nstderr="{stderr.decode()}')
            logger.error(msg)
            raise JobError(msg)

    async def run_job(self, job_request: V0JobRequest):
        try:
            docker_run_options = RunConfigManager.preset_to_docker_run_args(job_request.docker_run_options_preset)
            await self.unpack_volume(job_request)
        except JobError as ex:
            return JobResult(
                success=False,
                exit_status=None,
                timeout=False,
                stdout=ex.description,
                stderr="",
            )

        cmd = [
            'docker',
            'run',
            *docker_run_options,
            '--rm',
            '--network',
            'none',
            '-v',
            f'{volume_mount_dir.as_posix()}/:/volume/',
            '-v',
            f'{output_volume_mount_dir.as_posix()}/:/output/',
            job_request.docker_image_name,
            *job_request.docker_run_cmd,
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        t1 = time.time()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(),
                                                    timeout=self.initial_job_request.timeout_seconds)

        except TimeoutError:
            # If the process did not finish in time, kill it
            logger.error(f'Process didn\'t finish in time, killing it, job_uuid={self.initial_job_request.job_uuid}')
            process.kill()
            timeout = True
            exit_status = None
            stdout = (await process.stdout.read()).decode()
            stderr = (await process.stderr.read()).decode()
        else:
            stdout = stdout.decode()
            stderr = stderr.decode()
            exit_status = process.returncode
            timeout = False

        time_took = time.time() - t1
        success = exit_status == 0

        if success:
            logger.info(f'Job "{self.initial_job_request.job_uuid}" finished successfully in {time_took:0.2f} seconds')
        else:
            logger.error(f'"{" ".join(cmd)}" (job_uuid={self.initial_job_request.job_uuid})'
                         f' failed after {time_took:0.2f} seconds with status={process.returncode}'
                         f' \nstdout="{stdout}"\nstderr="{stderr}')

        return JobResult(
            success=success,
            exit_status=exit_status,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
        )

    async def _unpack_volume(self, job_request: V0JobRequest):
        assert str(volume_mount_dir) not in {'~', '/'}
        for path in volume_mount_dir.glob("*"):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

        if job_request.volume.volume_type == VolumeType.inline:
            decoded_contents = base64.b64decode(job_request.volume.contents)
            bytes_io = io.BytesIO(decoded_contents)
            zip_file = zipfile.ZipFile(bytes_io)
            zip_file.extractall(volume_mount_dir.as_posix())
        elif job_request.volume.volume_type == VolumeType.zip_url:
            with tempfile.NamedTemporaryFile() as download_file:
                async with httpx.AsyncClient() as client:
                    async with client.stream('GET', job_request.volume.contents) as response:
                        volume_size = int(response.headers["Content-Length"])
                        if 0 < settings.VOLUME_MAX_SIZE_BYTES < volume_size:
                            raise JobError("Input volume too large")

                        async for chunk in response.aiter_bytes():
                            download_file.write(chunk)
                download_file.seek(0)
                zip_file = zipfile.ZipFile(download_file)
                zip_file.extractall(volume_mount_dir.as_posix())
        else:
            raise NotImplementedError(f'Unsupported volume_type: {job_request.volume.volume_type}')

        chmod_proc = await asyncio.create_subprocess_exec("chmod", "-R", "777", temp_dir.as_posix())
        assert 0 == await chmod_proc.wait()

    async def unpack_volume(self, job_request: V0JobRequest):
        try:
            await asyncio.wait_for(self._unpack_volume(job_request), timeout=INPUT_VOLUME_UNPACK_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise JobError("Input volume downloading took too long") from exc


class Command(BaseCommand):
    help = 'Run the executor, query the miner for job details, and run the job docker'

    MINER_CLIENT_CLASS = MinerClient
    JOB_RUNNER_CLASS = JobRunner

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.loop = asyncio.get_event_loop()
        self.miner_client = self.MINER_CLIENT_CLASS(self.loop, settings.MINER_ADDRESS, settings.EXECUTOR_TOKEN)

    def handle(self, *args, **options):
        self.loop.run_until_complete(self._executor_loop())

    async def is_system_safe_for_cve_2022_0492(self):
        process = await asyncio.create_subprocess_exec(
            'docker',
            'run',
            'us-central1-docker.pkg.dev/twistlock-secresearch/public/can-ctr-escape-cve-2022-0492:latest',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), CVE_2022_0492_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.error('CVE-2022-0492 check timed out')
            return False

        if process.returncode != 0:
            logger.error(f'CVE-2022-0492 check failed: stdout="{stdout.decode()}"\nstderr="{stderr.decode()}')
            return False
        expected_output = 'Contained: cannot escape via CVE-2022-0492'
        if expected_output not in stdout.decode():
            logger.error(f'CVE-2022-0492 check failed: "{expected_output}" not in stdout.'
                         f'stdout="{stdout.decode()}"\nstderr="{stderr.decode()}')
            return False

        return True

    async def _executor_loop(self):
        logger.debug(f'Connecting to miner: {settings.MINER_ADDRESS}')
        async with self.miner_client:
            logger.debug(f'Connected to miner: {settings.MINER_ADDRESS}')
            initial_message: V0InitialJobRequest = await self.miner_client.initial_msg
            logger.debug('Checking for CVE-2022-0492 vulnerability')
            if not await self.is_system_safe_for_cve_2022_0492():
                await self.miner_client.send_failed_to_prepare()
                return
            try:
                job_runner = self.JOB_RUNNER_CLASS(initial_message)
                logger.debug(f'Preparing for job {initial_message.job_uuid}')
                try:
                    await job_runner.prepare()
                except JobError:
                    await self.miner_client.send_failed_to_prepare()
                    return

                logger.debug(f'Prepared for job {initial_message.job_uuid}')

                await self.miner_client.send_ready()
                logger.debug(f'Informed miner that I\'m ready for job {initial_message.job_uuid}')

                job_request = await self.miner_client.full_payload
                logger.debug(f'Running job {initial_message.job_uuid}')
                result = await job_runner.run_job(job_request)

                # Save the streams in output volume and truncate them in response.
                for field in ('stdout', 'stderr'):
                    value = getattr(result, field)
                    # TODO: Replace open() with async calls (aiofiles or something) if it becomes a async-bottleneck
                    with open(output_volume_mount_dir / f'{field}.txt', 'w') as f:
                        f.write(value)
                    setattr(result, field, truncate(value))

                if result.success:
                    if job_request.output_upload:
                        output_uploader = OutputUploader.for_upload_output(job_request.output_upload)
                        await output_uploader.upload(output_volume_mount_dir)
                    await self.miner_client.send_finished(result)
                else:
                    await self.miner_client.send_failed(result)
            except OutputUploadFailed as ex:
                logger.warning(f'Uploading output failed for job {initial_message.job_uuid} with error: {ex!r}')
                await self.miner_client.send_failed(JobResult(
                    success=False,
                    exit_status=None,
                    timeout=False,
                    stdout=ex.description,
                    stderr="",
                ))
            except Exception:
                logger.error(f'Unhandled exception when working on job {initial_message.job_uuid}', exc_info=True)
                # not deferred, because this is the end of the process, making it deferred would cause it never
                # to be sent
                await self.miner_client.send_generic_error('Unexpected error')

import concurrent.futures
import configparser
import enum
import logging
import pathlib
import signal
import shutil
import subprocess
import sys
import time
import typing

import attr
import click
import fabric
import paramiko

import generators
import shared


# TODO:     Add an option to download stats and Wireshark dumps via scp (fabric),
#           Adjust time and the process of running N senders concurrently,
#           Test the script on Windows with regard to ssh-agent,
#           Disbale password promt (fabric),
#           Find a way to insert carriage symbol "\r" at the end of log message,
#           Add running tshark remotely on a receiver side via SSH,
#           Add running sender side application remotely via SSH (?),
#           Merge start_sender, start_receiver functions in one,
#           Improve config parsing part,
#           Setup.py and better code structure,
#           Improve documentation,
#           Disable warnings from paramiko

# NOTE: The carriage "\r" is inserted in the end of log messages as
# a workaround for the problem with intended logs. This problem 
# appears as soon as receiver is started via SSH with a pseudo-terminal
# (ssh -t). It seems to be that something deletes "\r" symbol.
# https://serverfault.com/questions/593399/what-is-the-benefit-of-not-allocating-a-terminal-in-ssh
# FIXME: In order to avoid manually inserting "\r" symbol in each log message,
# there should be a way of formatiing logging messaging. However, quick 
# change of basicConfig to something like
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)-15s [%(levelname)s] %(message)s\n\r'
# )
# does not work.


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)-15s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)


@enum.unique
class TestName(shared.AutoName):
    bw_loop_test = enum.auto()
    filecc_loop_test = enum.auto()

TEST_NAMES = [name for name, member in TestName.__members__.items()]


def get_query(attrs_values):
    query_elements = []
    for attr, value in attrs_values:
        query_elements.append(f'{attr}={value}')
    return f'{"&".join(query_elements)}'


def start_sender(
    number,
    path_to_srt: str,
    host: str,
    port: str,
    attrs_values: typing.Optional[typing.List[typing.Tuple[str, str]]]=None,
    options_values: typing.Optional[typing.List[typing.Tuple[str, str]]]=None,
    description: str=None,
    collect_stats: bool=False,
    results_dir: pathlib.Path=None
):
    name = f'srt sender {number}'
    logger.info(f'Starting on a local machine: {name}\r')

    args = []
    args += [f'{path_to_srt}/srt-test-messaging']

    if attrs_values is not None:
        # FIXME: But here there is a problem with "" because sender has been
        # started locally, not via SSH
        args += [f'srt://{host}:{port}?{get_query(attrs_values)}']
    else:
        args += [f'srt://{host}:{port}']

    args += ['']

    if options_values is not None:
        for option, value in options_values:
            args += [option, value]

    if collect_stats:
        stats_file = results_dir / f'{description}-stats-snd-{number}.csv'
        args += [
            '-statsfreq', '1',
            '-statsfile', stats_file,
        ]
    
    snd_srt_process = shared.create_process(name, args)
    logger.info(f'Started successfully: {name}\r')
    return (name, snd_srt_process)


def start_receiver(
    ssh_host: str, 
    ssh_username: str, 
    path_to_srt: str,
    host: str,
    port: str,
    attrs_values: typing.Optional[typing.List[typing.Tuple[str, str]]]=None,
    options_values: typing.Optional[typing.List[typing.Tuple[str, str]]]=None,
    description: str=None,
    collect_stats: bool=False,
    results_dir: pathlib.Path=None
):
    """
    Starts srt-test-messaging application on a receiver side via SSH.

    Attributes:
        attrs_values:
            A list of SRT options (SRT URI attributes) in a format
            [('rcvbuf', '12058624'), ('smoother', 'live'), ('maxcon', '50')].
        options_values:
            A list of srt-test-messaging application options in a format
            [('-msgsize', '1456'), ('-reply', '0'), ('-printmsg', '0')].
    """
    name = 'srt receiver'
    logger.info(f'Starting {name} on a remote machine: {ssh_host}')
    args = []
    args += shared.SSH_COMMON_ARGS
    args += [
        f'{ssh_username}@{ssh_host}',
        f'{path_to_srt}/srt-test-messaging',
    ]

    if attrs_values is not None:
        # FIXME: There is a problem with "" here, if to run an app via SSH,
        # it does not work without ""
        args += [f'"srt://{host}:{port}?{get_query(attrs_values)}"']
    else:
        args += [f'srt://{host}:{port}']

    if options_values is not None:
        for option, value in options_values:
            args += [option, value]

    if collect_stats:
        args += ['-statsfreq', '1']
        stats_file = results_dir / f'{description}-stats-rcv.csv'
        args += ['-statsfile', stats_file]
    
    process = shared.create_process(name, args, True)
    logger.info('Started successfully\r')
    return (name, process)


def start_several_senders(
    quantity: int,
    mode: str,
    path_to_srt: str,
    host: str,
    port: str,
    attrs_values: typing.Optional[typing.List[typing.Tuple[str, str]]]=None,
    options_values: typing.Optional[typing.List[typing.Tuple[str, str]]]=None,
    description: str=None,
    collect_stats: bool=False,
    results_dir: pathlib.Path=None
):
    
    # FIXME: Transfer bitrate and repeat
    # logger.info(
    #     f'Starting streaming with bitrate {bitrate}, repeat {repeat}, '
    #     f'senders {snd_number}'
    # )
    logger.info(
        f'Starting streaming: {description}, '
        f'senders {quantity}\r'
    )

    sender_processes = []

    if quantity == 1 or mode == 'serial':
        for i in range(0, quantity):
            snd_srt_process = start_sender(
                i,
                path_to_srt,
                host,
                port,
                attrs_values,
                options_values,
                description,
                collect_stats,
                results_dir
            )
            sender_processes.append(snd_srt_process)

    if quantity != 1 and mode == 'parallel':
        with concurrent.futures.ThreadPoolExecutor(max_workers=quantity) as executor:
            # TODO: Change to list (?)
            future_senders = {
                executor.submit(
                    start_sender, 
                    i,
                    path_to_srt,
                    host,
                    port,
                    attrs_values,
                    options_values,
                    description,
                    collect_stats,
                    results_dir
                ): i for i in range(0, quantity)
            }

            errors = 0
            for future in concurrent.futures.as_completed(future_senders):
                try:
                    process = future.result()
                except Exception as exc:
                    logger.info(
                        f'{future_senders[future]} sender generated an '
                        f'exception: {exc}'
                    )
                    errors += 1
                else:
                    sender_processes.append(process)

            if errors > 0:
                raise shared.ParallelSendersExecutionFailed()

    return sender_processes


def perform_experiment(
    global_config,
    exper_params: generators.ExperimentParams,
    rcv: str,
    snd_quantity: int,
    snd_mode: str,
    collect_stats: bool=False,
    run_tshark: bool=False,
    results_dir: pathlib.Path=None
):
    """
    Performs one experiment.

    Returns:
        Extra time in seconds spent on SRT streaming.

    Raises:
        KeyboardInterrupt,
        shared.ProcessHasNotBeenStartedSuccessfully, 
        shared.ProcessHasNotBeenCreated,
        shared.ProcessHasNotBeenKilled
    """
    processes = []
    try:
        # Start SRT on a receiver side
        if rcv == 'remotely':
            rcv_srt_process = start_receiver(
                global_config.rcv_ssh_host, 
                global_config.rcv_ssh_username, 
                global_config.rcv_path_to_srt, 
                '',
                global_config.dst_port,
                exper_params.rcv_attrs_values,
                exper_params.rcv_options_values,
                exper_params.description,
                collect_stats,
                results_dir
            )
            processes.append(rcv_srt_process)
            time.sleep(3)

        # Start tshark on a sender side
        if run_tshark:
            filename = f'{exper_params.description}-snd.pcapng'
            snd_tshark_process = shared.start_tshark(
                global_config.snd_tshark_iface, 
                global_config.dst_port,
                results_dir,
                filename
            )
            processes.append(snd_tshark_process)
            time.sleep(3)

        # Start several SRT senders on a sender side to stream for
        # config.time_to_stream seconds
        sender_processes = start_several_senders(
            snd_quantity,
            snd_mode,
            global_config.snd_path_to_srt,
            global_config.dst_host,
            global_config.dst_port,
            exper_params.snd_attrs_values,
            exper_params.snd_options_values,
            exper_params.description,
            collect_stats,
            results_dir
        )
        for p in sender_processes:
            processes.append(p)

        # Sleep for config.time_to_stream seconds to wait while senders 
        # will finish the streaming and then check how many senders are 
        # still running.
        # FIXME: Time adjustment is needed for snd_mode='serial'
        time.sleep(exper_params.time_to_stream)
        extra_time = shared.calculate_extra_time(sender_processes)
        
        logger.info('Done\r')
        # time.sleep(3)
        return extra_time

        # if run_tshark:
        #     shared.cleanup_process(snd_tshark_process)
        #     time.sleep(3)
        # if rcv == 'remotely':
        #     shared.cleanup_process(rcv_srt_process)
        #     time.sleep(3)
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt has been caught')
        raise
    except (
        shared.ProcessHasNotBeenStartedSuccessfully, 
        shared.ProcessHasNotBeenCreated
    ) as error:
        logger.info(
            f'Exception occured ({error.__class__.__name__}): {error}'
        )
        raise
    finally:
        logger.info('Cleaning up\r')
        for process_tuple in reversed(processes):
            try:
                shared.cleanup_process(process_tuple)
            except shared.ProcessHasNotBeenKilled as error:
                # TODO: Collect the information regarding non killed processes
                # and perfom additional clean-up actions
                logger.info(
                    f'During cleaning up an exception occured '
                    f'({error.__class__.__name__}): {error}. The next '
                    f'experiment can not be done further!'
                )
                raise
        logger.info('Done')


@click.command()
@click.argument(
    'test_name',
    type=click.Choice(TEST_NAMES)
)
@click.argument(
    'config_filepath', 
    type=click.Path(exists=True)
)
@click.option(
    '--rcv', 
    type=click.Choice(['manually', 'remotely']), 
    default='remotely',
    help=	'Start receiver manually or remotely via SSH. In case of '
            'manual receiver start, please do not forget to do it '
            'before running the script.',
    show_default=True
)
@click.option(
    '--snd-quantity', 
    default=1,
    help=   'Number of senders to start.',
    show_default=True
)
@click.option(
    '--snd-mode',
    type=click.Choice(['serial', 'parallel']), 
    default='parallel',
    help=   'Start senders concurrently or in parallel.',
    show_default=True
)
@click.option(
    '--collect-stats', 
    is_flag=True, 
    help='Collect SRT statistics.'
)
@click.option(
    '--run-tshark',
    is_flag=True,
    help='Run tshark.'
)
@click.option(
    '--results-dir',
    default='_results',
    help=   'Directory to store results.',
    show_default=True
)
def main(
    test_name: str,
    config_filepath: str,
    rcv: str,
    snd_quantity: int,
    snd_mode: str,
    collect_stats: bool,
    run_tshark: bool,
    results_dir: typing.Optional[pathlib.Path]=None
):
    # FIXME: This is a temporary solution for being able to run main() function
    # outside this code. There is a problem with click:
    # (TypeError): main() takes from 1 to 5 positional arguments but 9 were 
    # given. File CC loop test can not be done.
    # FIXME: Also there is a problem with printing docstring description 
    # from main() function to terminal when running --help.
    main_function(
        test_name,
        config_filepath,
        rcv,
        snd_quantity,
        snd_mode,
        collect_stats,
        run_tshark,
        results_dir
    )

def main_function(
    test_name: str,
    config_filepath: str,
    rcv: str,
    snd_quantity: int,
    snd_mode: str,
    collect_stats: bool=False,
    run_tshark: bool=False,
    results_dir: typing.Optional[pathlib.Path]=None
):
    """ 
    Performs one test from the list of available tests `TEST_NAMES` 
    depending on the `test_name`. During the test, either one experiment
    or several experiments with different parameters are performed.
    Parameters for each experiment are generated by means of an 
    appropriate generator defined in `generators.py`.

    Global and tests specific settings are defined in config file 
    of type `config.ini`. 

    Attributes:
        test_name:
            Test name from the list of available tests `TEST_NAMES`.
        config_filepath:
            A path to a config file.
        rcv:
            Start a receiver manually or remotely via SSH.
        snd_quantity:
            Number of senders to start.
        snd_mode:
            Start senders concurrently or in parallel.
        collect_stats:
            True/False in case of collect/not collect SRT statistics.
        run_tsahrk:
            True/False in case of run/not run tshark on a sender side.
        results_dir:
            A path to a directory where test results should be stored.

    Returns a list of tuples of the following format
    (test description, bitrate, extra time needed to finish with streaming)

    Raises 
        paramiko.ssh_exception.SSHException if ssh-agent with an appropriate 
        RSA key has not been started in a terminal from which the script has
        been running.
    """
    config_filepath = pathlib.Path(config_filepath)
    results_dir = pathlib.Path(results_dir)
    global_config = generators.GlobalConfig.from_config_filepath(config_filepath)
    if test_name == TestName.bw_loop_test.value:
        test_config = generators.BandwidthLoopTestConfig.from_config_filepath(config_filepath)
        exper_params_generator = generators.bw_loop_test_generator(global_config, test_config)
    if test_name == TestName.filecc_loop_test.value:
        test_config = generators.FileCCLoopTestConfig.from_config_filepath(config_filepath)
        exper_params_generator = generators.filecc_loop_test_generator(global_config, test_config)

    try:
        if rcv == 'remotely':
            logger.info('Creating a folder for storing results on a receiver side')
            # FIXME: By default Paramiko will attempt to connect to a running 
            # SSH agent (Unix style, e.g. a live SSH_AUTH_SOCK, or Pageant if 
            # one is on Windows). That's why promt for login-password is not 
            # disabled under condition that password is not configured via 
            # connect_kwargs.password
            with fabric.Connection(host=global_config.rcv_ssh_host, user=global_config.rcv_ssh_username) as c:
                result = c.run(f'rm -rf {results_dir}')
                if result.exited != 0:
                    logger.info(f'Not created: {result}')
                    return
                result = c.run(f'mkdir -p {results_dir}')
                if result.exited != 0:
                    logger.info(f'Not created: {result}')
                    return
            logger.info('Created successfully')

        logger.info('Creating a folder for saving results on a sender side')
        results_dir = pathlib.Path(results_dir)
        if results_dir.exists():
            shutil.rmtree(results_dir)
        results_dir.mkdir(parents=True)
        logger.info('Created successfully')
    except paramiko.ssh_exception.SSHException as error:
        logger.info(
            f'Exception occured ({error.__class__.__name__}): {error}. '
            'Check that the ssh-agent has been started.'
        )
        raise
    except TimeoutError as error:
        logger.info(
            f'Exception occured ({error.__class__.__name__}): {error}. '
            'Check that IP address of the remote machine is correct and the '
            'machine is not down.'
        )
        raise

    result = []
    for exper_params in exper_params_generator:
        try:
            extra_time = perform_experiment(
                global_config,
                exper_params,
                rcv,
                snd_quantity,
                snd_mode,
                collect_stats,
                run_tshark,
                results_dir
            )
            logger.info(f'Extra time spent on streaming: {extra_time}')
        except (KeyboardInterrupt, shared.ProcessHasNotBeenKilled):
            break
        except (
            shared.ProcessHasNotBeenStartedSuccessfully, 
            shared.ProcessHasNotBeenCreated
        ) as error:
            continue

        result.append((
            exper_params.description,
            exper_params.bitrate,
            extra_time
        ))

        if extra_time >= 5:
            logger.info(
                f'Waited {exper_params.time_to_stream + extra_time} seconds '
                f'instead of {exper_params.time_to_stream}. '
                # f'{bitrate}bps is considered as maximim available bandwidth.'
            )
            # If it is a bandwidth loop test, there is no need to stream 
            # with the higher bitrate, because there is no available bandwidth
            if test_name == TestName.bw_loop_test.value:
                break

    return result


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import re
import os
import datetime
import logging
from threading import Event
import shlex
import sys
import zmq
import multiprocessing as mp

from sos.__main__ import get_run_parser
from sos.eval import SoS_exec
from sos.parser import SoS_Script
from sos.step_executor import PendingTasks
from sos.syntax import SOS_SECTION_HEADER
from sos.targets import (RemovedTarget, UnavailableLock,
                         UnknownTarget, sos_targets)
from sos.utils import _parse_error, env, get_traceback
from sos.workflow_executor import Base_Executor
from sos.executor_utils import prepare_env
from sos.controller import Controller, connect_controllers, disconnect_controllers
from sos.section_analyzer import analyze_section

from collections import defaultdict
from typing import Union, DefaultDict

from .step_executor import Interactive_Step_Executor



class NotebookLoggingHandler(logging.Handler):
    def __init__(self, level, kernel=None, title="Log Messages"):
        super(NotebookLoggingHandler, self).__init__(level)
        self.kernel = kernel
        self.title = title

    def setTitle(self, title):
        self.title = title

    def emit(self, record):
        msg = re.sub(r'``([^`]*)``',
                     r'<span class="sos_highlight">\1</span>', record.msg)
        self.kernel.send_frontend_msg('display_data', {
            'metadata': {},
            'data': {'text/html': f'<div class="sos_logging sos_{record.levelname.lower()}">{record.levelname}: {msg}</div>'}
        }, title=self.title, append=True, page='SoS')

def start_controller():
    env.zmq_context = zmq.Context()
    # ready to monitor other workflows
    env.config['tapping'] = 'master'

    ready = Event()
    controller = Controller(ready)
    controller.start()
    # wait for the thread to start with a signature_req saved to env.config
    ready.wait()
    connect_controllers(env.zmq_context)
    return controller

def stop_controller(controller):
    if not controller:
        return
    env.controller_req_socket.send_pyobj(['done'])
    env.controller_req_socket.recv()
    disconnect_controllers()
    controller.join()

def execute_scratch_cell(section, config):
    env.config['workflow_id'] = '0'
    env.sos_dict.set('workflow_id', '0')
    env.config.update(config)
    prepare_env('')

    # clear existing keys, otherwise the results from some random result
    # might mess with the execution of another step that does not define input
    for k in ['__step_input__', '__default_output__', '__step_output__']:
        env.sos_dict.pop(k, None)
    # if the step has its own context
    # execute section with specified input
    try:
        res = analyze_section(section)
        env.sos_dict.quick_update({
            '__signature_vars__': res['signature_vars'],
            '__environ_vars__': res['environ_vars'],
            '__changed_vars__': res['changed_vars']
        })
        executor = Interactive_Step_Executor(section, mode='interactive')
        return executor.run()
    except (UnknownTarget, RemovedTarget) as e:
        raise RuntimeError(f'Unavailable target {e.target}')



class Tapped_Executor(mp.Process):
    '''
    Worker process to process SoS step or workflow in separate process.
    '''

    def __init__(self, workflow, args=None, config={}, targets=None) -> None:
        # the worker process knows configuration file, command line argument etc
        super(Tapped_Executor, self).__init__()
        self.daemon = True
        #
        self.workflow = workflow
        self.args = args
        self.config = config
        self.targets = targets

    def run(self):
        from sos.utils import log_to_file

        try:
            self.config['verbosity'] = 4
            executor = Base_Executor(self.workflow, args=self.args, config=self.config)
            log_to_file('create executor')
            ret = executor.run(self.targets)
            env.tapping_logging_socket.send_multipart([b'info', b'DONE'])
            env.tapping_controller_socket.send_pyobj(ret)
            ret = env.tapping_controller.socket.recv()
            log_to_file('done')
        except Exception as e:
            log_to_file(str(e))
            env.tapping_logging_socket.send_multipart([b'info', str(e).encode()])

def run_sos_workflow(code=None, raw_args='', kernel=None, workflow_mode=False):

    # we then have to change the parse to disable args.workflow when
    # there is no workflow option.
    raw_args = shlex.split(raw_args) if isinstance(raw_args, str) else raw_args
    if code is None or '-h' in raw_args:
        parser = get_run_parser(interactive=True, with_workflow=True)
        parser.print_help()
        return
    if raw_args and raw_args[0].lstrip().startswith('-'):
        parser = get_run_parser(interactive=True, with_workflow=False)
        parser.error = _parse_error
        args, workflow_args = parser.parse_known_args(raw_args)
        args.workflow = None
    else:
        parser = get_run_parser(interactive=True, with_workflow=True)
        parser.error = _parse_error
        args, workflow_args = parser.parse_known_args(raw_args)

    # for reporting purpose
    sys.argv = ['%run'] + raw_args

    env.verbosity = args.verbosity
    if kernel and not isinstance(env.logger.handlers[0], NotebookLoggingHandler):
        env.logger.handlers = []
        levels = {
            0: logging.ERROR,
            1: logging.WARNING,
            2: logging.INFO,
            3: logging.DEBUG,
            4: logging.TRACE,
            None: logging.INFO
        }
        env.logger.addHandler(NotebookLoggingHandler(
            levels[env.verbosity], kernel, title=' '.join(sys.argv)))
    else:
        env.logger.handers[0].setTitle(' '.join(sys.argv))

    dt = datetime.datetime.now().strftime('%m%d%y_%H%M')
    if args.__dag__ is None:
        args.__dag__ = f'workflow_{dt}.dot'
    elif args.__dag__ == '':
        args.__dag__ = None

    if args.__report__ is None:
        args.__report__ = f'workflow_{dt}.html'
    elif args.__report__ == '':
        args.__report__ = None

    if args.__remote__:
        from sos.utils import load_config_files
        cfg = load_config_files(args.__config__)
        env.sos_dict.set('CONFIG', cfg)

        # if executing on a remote host...
        from sos.hosts import Host
        host = Host(args.__remote__)
        #
        if not code.strip():
            return
        script = os.path.join('.sos', '__interactive__.sos')
        with open(script, 'w') as s:
            s.write(code)

        # copy script to remote host...
        print(f'HINT: Executing workflow on {args.__remote__}')
        host.send_to_host(script)
        from sos.utils import remove_arg
        argv = shlex.split(raw_args) if isinstance(raw_args, str) else raw_args
        argv = remove_arg(argv, '-r')
        argv = remove_arg(argv, '-c')
        # execute the command on remote host
        try:
            with kernel.redirect_sos_io():
                ret = host._host_agent.run_command(['sos', 'run', script] + argv, wait_for_task=True,
                                                   realtime=True)
            if ret:
                kernel.send_response(kernel.iopub_socket, 'stream',
                                     dict(name='stderr',
                                          text=f'remote execution of workflow exited with code {ret}'))
        except Exception as e:
            if kernel:
                kernel.send_response(kernel.iopub_socket, 'stream',
                                     {'name': 'stdout', 'text': str(e)})
        return

    if args.__bin_dirs__:
        for d in args.__bin_dirs__:
            if d == '~/.sos/bin' and not os.path.isdir(os.path.expanduser(d)):
                os.makedirs(os.path.expanduser(d), exist_ok=True)
        os.environ['PATH'] = os.pathsep.join(
            [os.path.expanduser(x) for x in args.__bin_dirs__]) + os.pathsep + os.environ['PATH']

    # clear __step_input__, __step_output__ etc because there is
    # no concept of passing input/outputs across cells.
    env.sos_dict.set('__step_output__', sos_targets([]))
    for k in ['__step_input__', '__default_output__', 'step_input', 'step_output',
              'step_depends', '_input', '_output', '_depends']:
        env.sos_dict.pop(k, None)

    config = {
        'config_file': args.__config__,
        'output_dag': args.__dag__,
        'output_report': args.__report__,
        'sig_mode': 'ignore' if args.dryrun else args.__sig_mode__,
        'default_queue': '' if args.__queue__ is None else args.__queue__,
        'wait_for_task': True if args.__wait__ is True or args.dryrun else (False if args.__no_wait__ else None),
        'resume_mode': False,
        'run_mode': 'dryrun' if args.dryrun else 'interactive',
        'verbosity': args.verbosity,

        # wait if -w or in dryrun mode, not wait if -W, otherwise use queue default
        'max_procs': args.__max_procs__,
        'max_running_jobs': args.__max_running_jobs__,
        # for infomration and resume only
        'workdir': os.getcwd(),
        'script': "interactive",
        'workflow': args.workflow,
        'targets': args.__targets__,
        'bin_dirs': args.__bin_dirs__,
        'workflow_args': workflow_args
    }

    try:
        if not code.strip():
            return
        if workflow_mode:
            # in workflow mode, the content is sent by magics %run and %sosrun
            script = SoS_Script(content=code)
            workflow = script.workflow(
                args.workflow, use_default=not args.__targets__)
            config['tapping'] = 'slave'
            config['sockets'] = {
                'tapping_logging': env.config['sockets']['tapping_logging'],
                'tapping_controller': env.config['sockets']['tapping_controller']
            }
            executor = Tapped_Executor(workflow, args=workflow_args, config=config,
                targets=args.__targets__)
            executor.start()
            return
        else:
            env.config['master_id'] = '0'
            # this is a scratch step...
            # if there is no section header, add a header so that the block
            # appears to be a SoS script with one section
            if not any([SOS_SECTION_HEADER.match(line) or line.startswith('%from') or line.startswith('%include') for line in code.splitlines()]):
                code = '[scratch_0]\n' + code
                script = SoS_Script(content=code)
            else:
                return
            workflow = script.workflow(args.workflow)
            return execute_scratch_cell(workflow.sections[0], config=config)['__last_res__']

    except PendingTasks:
        raise
    except SystemExit:
        # this happens because the executor is in resume mode but nothing
        # needs to be resumed, we simply pass
        return
    except Exception:
        if args.verbosity and args.verbosity > 2:
            sys.stderr.write(get_traceback())
        raise
    finally:
        env.config['sig_mode'] = 'ignore'
        env.verbosity = 2

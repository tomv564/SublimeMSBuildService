import sublime
import sublime_plugin
import os
import json
import threading

from threading import Thread
from typing import Optional, Awaitable
import asyncio

__loop: Optional[asyncio.AbstractEventLoop] = None
__thread: Optional[Thread] = None

build_service_client=None

CONTENT_LENGTH = 'Content-Length: '


class BuildServiceInstance():
    """
        bool BuildProject(string projectName, string configurationName);
        bool BuildSolution(string configurationName);
        ProjectFileInfo FindProjectForFile(string filePath);
        bool LoadSolution(string filePath);
    """
    def __init__(self):
        self.cmd = r"C:\Users\Tom\Projects\MSBuildService\MSBuildServiceHost\bin\Debug\MSBuildServiceHost.exe"
        self.solution_loaded = False
        self.process = None
        self.panel = None
        self.requesting = False

    async def start(self, solution_path):
        self.process = await asyncio.create_subprocess_shell(
                   self.cmd,
                   stdout=asyncio.subprocess.PIPE,
                   stdin=asyncio.subprocess.PIPE)
        print('process started, loading ' + solution_path)
        return await self.load_solution(solution_path)

    def stop(self):
        if self.process:
            self.process.kill()

    async def _wait_for_response(self):
        while True:

            header_data = await self.process.stdout.readline()
            header = header_data.decode('UTF-8').rstrip()
            # print("<<< " + header)
            if len(header) > len(CONTENT_LENGTH):
                content_size = int(header[len(CONTENT_LENGTH):])
                await self.process.stdout.readline()

                response_data = await self.process.stdout.read(content_size)
                response = response_data.decode('UTF-8').rstrip()
                print("<<< " + response)
                response_obj = json.loads(response)
                if 'error' in response_obj:
                    return response_obj
                if 'result' in response_obj:
                    return response_obj
                if 'params' in response_obj:
                    log_message = response_obj['params'][0]['Message']
                    print('<<< ' + str(log_message))
                    if self.panel:
                        # self.panel.run_command('append', {'characters': log_message.rstrip()})
                        self.queue_write(log_message.rstrip('\r\n') + '\n')
                    else:
                        print("no panel!")


    async def request(self, payload):
        if not self.process:
            return

        if self.requesting:
            print("--- request already in progress, skipping")
            return

        self.requesting = True

        content = json.dumps(payload)

        print(">>> " + content)

        header = CONTENT_LENGTH + str(len(content))
        msg = header + '\r\n\r\n' + content
        # print(msg)
        self.process.stdin.write(msg.encode('UTF-8'))
        # self.process.stdin.drain()

        response = await self._wait_for_response()

        # self.panel = None
        self.requesting = False

        if 'result' in response:
            return response['result']
        else:
            return False

    def queue_write(self, text):
        sublime.set_timeout_async(lambda: self.do_write(text), 1)

    def do_write(self, text):
        # with self.panel_lock:
        if self.panel:
            self.panel.run_command('append', {'characters': text, "scroll_to_end": False})

    
    async def get_project_name(self, file_path):
        req = build_request("FindProjectForFile", {'filePath': file_path.replace('\\', '/')})
        result = await self.request(req)
        print('get project name returned: ' + str(result))
        return result

    async def load_solution(self, solution_path):
        req = build_request("LoadSolution" ,{'filePath': solution_path.replace('\\', '/')})
        result = await self.request(req)
        print('load solution returned: ' + str(result))
        if result:
            self.solution_loaded = True
        return result

    async def build_project(self, project_name, config_name, panel):
        self.panel = panel
        req = build_request("BuildProject", {'projectName': project_name, 'configurationName': config_name})
        result = await self.request(req)
        print('build project returned: ' + str(result))
        return result

    async def build_file(self, file_name, project_name, config_name, panel):
        self.panel = panel
        req = build_request("BuildFile", {'filePath': file_name, 'projectName': project_name, 'configurationName': config_name})
        result = await self.request(req)
        print('build project returned: ' + str(result))
        return result

    async def build_solution(self, config_name, panel):
        self.panel = panel
        req = build_request("BuildSolution", {'configurationName': config_name})
        result = await self.request(req)
        print('build project returned: ' + str(result))
        return result



def plugin_loaded() -> None:
    print("loop: starting")
    global __loop
    global __thread
    __loop = asyncio.new_event_loop()
    __thread = Thread(target=__loop.run_forever)
    __thread.start()
    print("loop: started")


def __shutdown() -> None:
    for task in asyncio.Task.all_tasks():
        task.cancel()
    asyncio.get_event_loop().stop()


def plugin_unloaded() -> None:


    print("loop: stopping")
    global __loop
    global __thread
    if __loop and __thread:
        __loop.call_soon_threadsafe(__shutdown)
        __thread.join()
        __loop.run_until_complete(__loop.shutdown_asyncgens())
        __loop.close()
    __loop = None
    __thread = None
    print("loop: stopped")
    for build_service_client in service_by_window.values():
        build_service_client.stop()


def schedule(coro: Awaitable) -> None:
    global __loop
    if __loop:
        __loop.call_soon_threadsafe(asyncio.ensure_future, coro)

request_id = 0

def build_request(method, params):
    global request_id
    request_id += 1
    return dict(jsonrpc='2.0', id=request_id, method=method, params=params)


service_by_window = {}

def get_client(window):
    if window.id() in service_by_window:
        return service_by_window[window.id()]
    return None


# https://github.com/sublimehq/sublime_text/issues/3129


def get_msbuild_settings(window):
    data = window.project_data()
    if 'settings' in data:
        settings = data['settings']
        if 'MSBuildService' in settings:
            return settings['MSBuildService']
    return None

def get_solution_path(window):
    settings = get_msbuild_settings(window)
    if settings:
        return settings['solution_path']
    return None

def get_configuration_name(window):
    settings = get_msbuild_settings(window)
    if settings:
        return settings['configuration_name']
    return None


class BuildContextListener(sublime_plugin.EventListener):

    def on_activated_async(self, view):
        if view.file_name() is None:
            return

        file_name = os.path.abspath(view.file_name())
        # print("activated: " + file_name)
        client = get_client(view.window())

        # todo: lock/mutex
        if not client:
            solution_path = get_solution_path(view.window())
            if solution_path:
                if os.path.exists(solution_path):                
                    print('project has solution: ' + solution_path)
                    schedule(self.start_session(view, solution_path))
                else:
                    print('project solution path does not exist: ' + solution_path)
        else:
            project_name = view.settings().get('msbuild_project_name')
            if project_name is None:
                if client.solution_loaded:
                    schedule(self.get_project_info(client, file_name, view))

        # r"C:\Users\Tom\Projects\MSBuildService\MSBuildService.sln"

    async def get_project_info(self, client, file_path, view):
        project_info = await client.get_project_name(file_path)
        if project_info:
            project_name = project_info['ProjectName']
            view.set_status("msbuild_project_name", project_name)
            view.settings().set("msbuild_project_name", project_name)

    async def start_session(self, view, solution_path):
        window = view.window()
        client = BuildServiceInstance()
        result = await client.start(solution_path)
        if result:
            service_by_window[window.id()] = client
            print('msbuild service loaded solution: ' + solution_path)


class MsbuildServiceFileCommand(sublime_plugin.WindowCommand):

    killed = False
    panel = None
    panel_lock = threading.Lock()

    def is_enabled(self, solution=False, project=False, file=False, kill=False):
        # The Cancel build option should only be available
        # when the process is still running
        # if kill:
        #     return self.proc is not None and self.proc.poll() is None
        if file:
            return False

        return True

    def run(self, solution=False, project=False, file=False, kill=False):
        file_name = os.path.abspath(self.window.active_view().file_name())
        print("build: " + file_name)

        client = get_client(self.window)
        if not client:
            print("no client")
            return

        configuration_name = get_configuration_name(self.window) or 'Debug'

        # todo: lock/mutex
        if client.solution_loaded:

            project_name = None
            if self.window.active_view():
                project_name = self.window.active_view().settings().get('msbuild_project_name')


            panel_lock = threading.Lock()

            with panel_lock:
                self.panel = self.window.create_output_panel('exec')

                settings = self.panel.settings()
                settings.set('line_numbers', False)
                settings.set('scroll_past_end', False)

                self.panel = self.window.create_output_panel('exec')
                # self.panel.set_read_only(True)

                self.window.run_command('show_panel', {'panel': 'output.exec'})

            if solution:
                schedule(self.build_solution(client, configuration_name, self.panel))
            elif project:
                schedule(self.build_project(client, project_name, configuration_name, self.panel))
            elif file:
                schedule(self.build_file(client, file_name, project_name, configuration_name, self.panel))

        # else:
            # schedule(client.load_solution(r"C:\Users\Tom\Projects\MSBuildService\MSBuildService.sln"))

    async def build_solution(self, client, configuration_name, panel):

        result = await client.build_solution(configuration_name, panel)
        print("build result" + str(result))\

    async def build_project(self, client, project_name, configuration_name, panel):
        result = await client.build_project(project_name, configuration_name, panel)
        print("build result" + str(result))

    async def build_file(self, client, file_name, project_name, configuration_name, panel):

        result = await client.build_file(file_name, project_name, configuration_name, panel)
        print("build result" + str(result))

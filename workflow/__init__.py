from enum import Enum
from typing import Callable, Optional
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

import inspect
import asyncio


class Scope(Enum):
    NORMAL = 'normal'
    ERROR = 'error'
    EXIT = 'exit'


class ExecutionMode(Enum):
    THREAD = 'thread'
    PROCESS = 'process'


class Workflow:
    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self.ctx['cancel'] = False
        self.ctx['error'] = False
        self.thread_executor = ThreadPoolExecutor()
        self.process_executor = ProcessPoolExecutor()
        self.steps = {
            Scope.NORMAL.value: [],
            Scope.ERROR.value: [],
            Scope.EXIT.value: []
        }

    def __exit__(self, exc_type, exc_value, traceback):
        self.thread_executor.shutdown(wait=True)
        self.process_executor.shutdown(wait=True)

    def add_step(self,
                 func: Callable,
                 scope=Scope.NORMAL,
                 name: Optional[str] = None,
                 timeout: Optional[int] = None,
                 retries: int = 0,
                 cont_on_err: bool = False):
        if name is None:
            name = func.__name__
        self.steps[scope.value].append({
            'name': name,
            'func': func,
            'timeout': timeout,
            'retries': retries,
            'cont_on_err': cont_on_err
        })

    def add_error_step(self,
                       func: Callable,
                       name: Optional[str] = None,
                       timeout: Optional[int] = None,
                       retries: int = 0):
        self.add_step(
            func,
            Scope.ERROR,
            name,
            timeout,
            retries
        )

    def add_exit_step(self,
                      func: Callable,
                      name: Optional[str] = None,
                      timeout: Optional[int] = None,
                      retries: int = 0):
        self.add_step(
            func,
            Scope.EXIT,
            name,
            timeout,
            retries
        )

    def add_cond_step(
            self,
            name: str,
            on_true_step: Callable,
            on_false_step: Callable,
            scope=Scope.NORMAL,
            timeout: Optional[int] = None,
            retries: int = 0):
        async def cond_step():
            prev_result = self.ctx.get(
                self.steps[scope.value][-2]['name']) if self.steps[scope.value] else None
            step_func = on_true_step if prev_result else on_false_step
            return await self._run_step({
                'func': step_func,
                'timeout': timeout,
                'retries': retries
            })
        self.add_step(
            cond_step,
            scope,
            name,
            timeout,
            retries
        )

    def add_parallel_steps(
            self,
            name: str,
            steps: list[Callable],
            execution_mode=ExecutionMode.THREAD,
            scope=Scope.NORMAL,
            timeout: Optional[int] = None,
            retries: int = 0):
        async def parallel_step():
            tasks = []
            executor = self.thread_executor if execution_mode == ExecutionMode.THREAD else self.process_executor

            for func in steps:
                args = self._get_step_args(func)
                if asyncio.iscoroutinefunction(func):
                    task = asyncio.ensure_future(func(*args))
                else:
                    loop = asyncio.get_running_loop()
                    task = loop.run_in_executor(executor, func, *args)

                tasks.append(task)

            return await asyncio.gather(*tasks)

        self.add_step(
            parallel_step,
            scope,
            name,
            timeout,
            retries
        )

    async def run_steps(self, scope: Scope):
        steps = self.steps[scope.value]
        for step in steps:
            try:
                if self.ctx.get('cancel', False):
                    break

                result = await self._run_step(step)
                self.ctx[step['name']] = result
            except Exception as e:
                self.ctx[f'{scope.value}_error'] = {
                    'step': step['name'],
                    'error': e
                }
                if not step['cont_on_err']:
                    raise e

    async def run(self, **kwargs):
        self.ctx.update(kwargs)
        try:
            await self.run_steps(Scope.NORMAL)
        except Exception as e:
            self.ctx['error'] = True
            if len(self.steps[Scope.ERROR.value]) == 0 and len(self.steps[Scope.EXIT.value]) == 0:
                raise e
            else:
                await self.run_steps(Scope.ERROR)
        finally:
            await self.run_steps(Scope.EXIT)

    def cancel(self):
        self.ctx['cancel'] = True

    def _get_step_args(self, func: Callable):
        func_args = inspect.signature(func).parameters
        if 'ctx' in func_args:
            return [self.ctx]
        else:
            return [self.ctx[arg] for arg in func_args]

    async def _run_step(self, step: dict):
        func = step['func']
        timeout = step.get('timeout')
        retries = step.get('retries', 0)
        args = self._get_step_args(func)

        for i in range(retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    if timeout is not None:
                        return await asyncio.wait_for(func(*args), timeout=timeout)
                    else:
                        return await func(*args)
                else:
                    return func(*args)
            except Exception as e:
                if i == retries:
                    raise e

# (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    name: debug
    type: stdout
    short_description: formatted stdout/stderr display
    description:
      - Use this callback to sort through extensive debug output
    extends_documentation_fragment:
      - default_callback
    requirements:
      - set as stdout in configuration
'''

from ansible.plugins.callback.default import CallbackModule as CallbackModule_default


class CallbackModule(CallbackModule_default):  # pylint: disable=too-few-public-methods
    '''
    Override for the default callback module.

    Render std err/out outside of the rest of the result which it prints with
    indentation.
    '''
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'ansible.posix.debug'

    def __init__(self):
        super().__init__()
        self._reported_failure_details = set()

    def _display_failure_details(self, result, label):
        result_data = result._result
        if (
            result_data.get('censored')
            or result_data.get('_ansible_no_log')
            or getattr(result._task, 'no_log', False)
        ):
            return

        detail_lines = []
        for key in ('rc', 'status', 'msg', 'stderr', 'stdout', 'module_stderr', 'module_stdout'):
            value = result_data.get(key)
            if value is not None and value != '':
                detail_lines.append('%s:\n%s' % (key.upper(), value))

        if not detail_lines:
            return

        details = '\n'.join(detail_lines)
        detail_key = (result._host.get_name(), result._task._uuid, details)
        if detail_key in self._reported_failure_details:
            return
        self._reported_failure_details.add(detail_key)

        task_name = result.task_name or result._task
        attempt = result_data.get('attempts', 'unknown')
        self._display.display(
            '%s: [%s]: %s (attempt %s)\n%s'
            % (label, self.host_label(result), task_name, attempt, details)
        )

    def v2_runner_retry(self, result):
        super().v2_runner_retry(result)
        if self._display.verbosity <= 2:
            self._display_failure_details(result, 'RETRY OUTPUT')

    @staticmethod
    def _has_nonzero_result(result_data):
        if result_data.get('rc') not in (None, 0):
            return True

        try:
            return int(result_data.get('status', 0)) >= 400
        except (TypeError, ValueError):
            return False

    def v2_runner_on_ok(self, result):
        super().v2_runner_on_ok(result)
        if self._display.verbosity <= 2 and self._has_nonzero_result(result._result):
            self._display_failure_details(result, 'ANSIBLE CONTINUED AFTER NONZERO RESULT')

    def v2_runner_item_on_ok(self, result):
        super().v2_runner_item_on_ok(result)
        if self._display.verbosity <= 2 and self._has_nonzero_result(result._result):
            self._display_failure_details(result, 'ANSIBLE CONTINUED AFTER NONZERO RESULT')

    def _dump_results(self, result, indent=None, sort_keys=True, keep_invocation=False):
        '''Return the text to output for a result.'''

        # Enable JSON identation
        result['_ansible_verbose_always'] = True

        save = {}
        for key in ['stdout', 'stdout_lines', 'stderr', 'stderr_lines', 'msg', 'module_stdout', 'module_stderr']:
            if key in result:
                save[key] = result.pop(key)

        output = CallbackModule_default._dump_results(self, result)

        for key in ['stdout', 'stderr', 'msg', 'module_stdout', 'module_stderr']:
            if key in save and save[key]:
                output += '\n\n%s:\n\n%s\n' % (key.upper(), save[key])

        for key, value in save.items():
            result[key] = value

        return output

import unittest

from app.library import _sanitize_component, _format_nsz_command


class LibraryHelperTests(unittest.TestCase):
    def test_sanitize_component(self):
        self.assertEqual(_sanitize_component('Game: Name?'), 'Game Name')
        self.assertEqual(_sanitize_component(''), 'Unknown')

    def test_format_nsz_command_threads(self):
        command = _format_nsz_command(
            '{nsz_exe} -C -o "{output_dir}" "{input_file}"',
            'C:\\input.nsp',
            'C:\\output.nsz',
            threads=4
        )
        self.assertIn('-t 4', command)
        self.assertIn('input.nsp', command)


if __name__ == '__main__':
    unittest.main()

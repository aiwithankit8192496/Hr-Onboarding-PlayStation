import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"

# Run in a child process: when pdfium is hit from two threads at once it corrupts the heap and
# kills the whole process (this is what took the onboarding server down on "Fetch bank details")
SCRIPT = r"""
import io, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, sys.argv[1])
import pypdfium2
from hr_onboarding import employee_portal

def make_pdf(size):
    pdf = pypdfium2.PdfDocument.new()
    pdf.new_page(size, size)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()

payslip, letter = make_pdf(595), make_pdf(612)

def work(pdf):
    assert employee_portal.pdf_to_images(pdf, max_pages=1)
    employee_portal.pdf_text(pdf, max_pages=1)

with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(work, [payslip, letter] * 100))
print("ok")
"""


class PdfThreadSafetyTests(unittest.TestCase):

    def test_concurrent_pdf_reads_do_not_crash_the_process(self):
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            self.skipTest("pypdfium2 is not installed")

        result = subprocess.run(
            [sys.executable, "-I", "-c", SCRIPT, str(ROOT)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"process died (exit {result.returncode}): {result.stderr[-300:]}")
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()

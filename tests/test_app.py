import csv
import os
import re
import shutil
import unittest
import uuid

import quiz_web


class QuizWebTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = os.path.join(os.getcwd(), ".test_tmp", f"biol112_test_{uuid.uuid4().hex}")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.registry_path = os.path.join(self.tmpdir, "chapters.csv")
        self.session_dir = os.path.join(self.tmpdir, "flask_session")
        os.makedirs(self.session_dir, exist_ok=True)

        self.old_registry = quiz_web.CHAPTERS_REGISTRY
        quiz_web.CHAPTERS_REGISTRY = self.registry_path

        quiz_web.app.config["TESTING"] = True
        quiz_web.app.config["SECRET_KEY"] = "test-secret"
        quiz_web.app.config["SESSION_FILE_DIR"] = self.session_dir

        self.client = quiz_web.app.test_client()

    def tearDown(self):
        quiz_web.CHAPTERS_REGISTRY = self.old_registry
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_registry(self, rows):
        with open(self.registry_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "exam_id",
                    "exam_title",
                    "chapter_id",
                    "chapter_title",
                    "order",
                    "quiz_file",
                ]
            )
            writer.writerows(rows)

    @staticmethod
    def _csrf_token(html):
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return m.group(1) if m else ""

    def test_load_questions_format_a(self):
        quiz_path = os.path.join(self.tmpdir, "quiz.csv")
        with open(quiz_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Question ID",
                    "Question Text",
                    "Option 1",
                    "Option 2",
                    "Option 3",
                    "Correct Answer",
                ]
            )
            writer.writerow(["Q1", "What is 2+2?", "3", "4", "5", "2"])

        questions = quiz_web.load_questions_from_file(quiz_path)
        self.assertIn("[Q1] What is 2+2?", questions)
        self.assertEqual(questions["[Q1] What is 2+2?"]["alternatives"][0], "4")

    def test_load_questions_accepts_image_link_column(self):
        quiz_path = os.path.join(self.tmpdir, "quiz_with_image_link.csv")
        with open(quiz_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Question Text",
                    "Option 1",
                    "Option 2",
                    "Correct Answer",
                    "Image Link",
                ]
            )
            writer.writerow(
                [
                    "Identify the structure.",
                    "Leaf",
                    "Root",
                    "1",
                    "static/images/example.png",
                ]
            )

        questions = quiz_web.load_questions_from_file(quiz_path)
        self.assertEqual(
            questions["Identify the structure."]["image"],
            "static/images/example.png",
        )

    def test_quiz_lifecycle(self):
        quiz_path = os.path.join(self.tmpdir, "chapter1_quiz.csv")
        with open(quiz_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Question ID",
                    "Question Text",
                    "Option 1",
                    "Option 2",
                    "Correct Answer",
                ]
            )
            writer.writerow(["1", "Sky color?", "Blue", "Red", "1"])

        self._write_registry(
            [
                [
                    "Exam 1",
                    "Exam 1",
                    "Chapter 1",
                    "Intro",
                    "1",
                    quiz_path,
                ]
            ]
        )

        r1 = self.client.get("/chapter/chapter1/quiz")
        self.assertEqual(r1.status_code, 200)
        token = self._csrf_token(r1.get_data(as_text=True))
        self.assertTrue(token)

        r2 = self.client.post(
            "/chapter/chapter1/quiz",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertIn("Sky color?", r2.get_data(as_text=True))

        r3 = self.client.post(
            "/chapter/chapter1/submit",
            data={"answer": "Blue", "csrf_token": token},
            follow_redirects=True,
        )
        self.assertEqual(r3.status_code, 200)
        self.assertIn("Correct", r3.get_data(as_text=True))

        token2 = self._csrf_token(r3.get_data(as_text=True))
        r_jump = self.client.post(
            "/chapter/chapter1/goto",
            data={"question_number": "1", "csrf_token": token2},
            follow_redirects=True,
        )
        self.assertEqual(r_jump.status_code, 200)
        self.assertIn("Sky color?", r_jump.get_data(as_text=True))

        r4 = self.client.get("/chapter/chapter1/next", follow_redirects=True)
        self.assertEqual(r4.status_code, 200)
        self.assertIn("Results", r4.get_data(as_text=True))

    def test_missing_files_render_fallback_messages(self):
        missing_quiz = os.path.join(self.tmpdir, "missing_quiz.csv")
        self._write_registry(
            [
                [
                    "Exam 1",
                    "Exam 1",
                    "Chapter 2",
                    "Missing Content",
                    "1",
                    missing_quiz,
                ]
            ]
        )

        rq = self.client.get("/chapter/chapter2/quiz")
        self.assertEqual(rq.status_code, 200)
        self.assertIn("Quiz content unavailable for this chapter.", rq.get_data(as_text=True))



if __name__ == "__main__":
    unittest.main()

from flask import Flask, render_template, request, session, redirect, url_for, abort
from flask_session import Session
import pandas as pd
import random
import os
import re

app = Flask(__name__)
app.secret_key = "biology-quiz-secret-2026"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Registry file
CHAPTERS_REGISTRY = os.path.join(BASE_DIR, "chapters.csv")

# Server-side sessions
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = os.path.join(BASE_DIR, "flask_session")
app.config["SESSION_PERMANENT"] = False
Session(app)


# -----------------------------
# Utilities
# -----------------------------
def norm_id(s: str) -> str:
    """
    Normalize IDs for URLs/lookup:
    "Chapter 22" -> "chapter22"
    "Exam 1" -> "exam1"
    """
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def abs_path(p: str) -> str:
    """
    Convert relative path (from registry) to absolute path.
    Empty => "".
    """
    p = (p or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    return os.path.join(BASE_DIR, p)


def load_table_any(path: str) -> pd.DataFrame:
    """
    Loads CSV/XLSX/XLS into a DataFrame.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(path)
        df.columns = df.columns.astype(str).str.strip()
        return df

    # Excel
    try:
        df = pd.read_excel(path, sheet_name=0)
    except Exception as e1:
        if ext == ".xls":
            try:
                df = pd.read_excel(path, sheet_name=0, engine="xlrd")
            except Exception as e2:
                raise RuntimeError(
                    f"Failed to read .xls. Convert to .xlsx or install xlrd==1.2.0. Errors: {e1} | {e2}"
                )
        else:
            raise

    df.columns = df.columns.astype(str).str.strip()
    return df


# -----------------------------
# Registry: chapters.csv
# -----------------------------
def load_registry_df() -> pd.DataFrame:
    if not os.path.exists(CHAPTERS_REGISTRY):
        raise RuntimeError("chapters.csv not found in project folder.")

    df = pd.read_csv(CHAPTERS_REGISTRY)
    df.columns = df.columns.astype(str).str.strip()

    required = [
        "exam_id",
        "exam_title",
        "chapter_id",
        "chapter_title",
        "order",
        "quiz_file",
        "flashcards_file",
        "datatable_file",
        "resources_file",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"chapters.csv missing columns: {missing}")

    # Strip whitespace
    for c in ["exam_id", "exam_title", "chapter_id", "chapter_title", "quiz_file",
              "flashcards_file", "datatable_file", "resources_file"]:
        df[c] = df[c].astype(str).str.strip()

    # Normalize keys used by routes
    df["exam_key"] = df["exam_id"].apply(norm_id)
    df["chapter_key"] = df["chapter_id"].apply(norm_id)

    # Order as int if possible
    def to_int(x):
        try:
            return int(float(x))
        except Exception:
            return 999999

    df["order_num"] = df["order"].apply(to_int)

    return df


def build_exams():
    """
    Returns list like:
    [
      {"key":"exam1","title":"EXAM1","chapters":[{"key":"chapter22","title":"Evolution ..."}, ...]},
      ...
    ]
    """
    df = load_registry_df()
    exams = []
    for exam_key, g in df.groupby("exam_key", sort=False):
        # pick title from first row
        exam_title = g.iloc[0]["exam_title"]

        ch_list = []
        g2 = g.sort_values(["order_num", "chapter_key"])
        for _, r in g2.iterrows():
            ch_list.append({
                "key": r["chapter_key"],
                "title": r["chapter_title"],
            })

        exams.append({
            "key": exam_key,
            "title": exam_title,
            "chapters": ch_list,
        })

    # Keep original order from file as much as possible
    return exams


def get_chapter_by_key(chapter_key: str):
    df = load_registry_df()
    chapter_key = norm_id(chapter_key)

    rows = df[df["chapter_key"] == chapter_key]
    if rows.empty:
        return None

    r = rows.iloc[0]
    return {
        "key": r["chapter_key"],
        "id_raw": r["chapter_id"],          # original (e.g., "Chapter 22")
        "title": r["chapter_title"],        # display title
        "exam_key": r["exam_key"],
        "exam_title": r["exam_title"],
        "quiz_file": abs_path(r["quiz_file"]),
        "flashcards_file": abs_path(r["flashcards_file"]),
        "datatable_file": abs_path(r["datatable_file"]),
        "resources_file": abs_path(r["resources_file"]),
    }


# -----------------------------
# Quiz loader (CSV/Excel with your columns)
# -----------------------------
def load_questions_from_file(path: str) -> dict:
    """
    Format A (your current):
      Question ID, Question Text, Option 1..Option 5, Correct Answer

    Also supports Format B (older) for safety:
      Question, Option A..E, Answer

    Returned dict key is the DISPLAY question string (includes Question ID if available).
    """
    if not path or not os.path.exists(path):
        raise RuntimeError(f"Quiz file not found: {path}")

    df = load_table_any(path)
    df.columns = df.columns.astype(str).str.strip()

    has_new = ("Question Text" in df.columns) and ("Correct Answer" in df.columns)
    has_old = ("Question" in df.columns) and ("Answer" in df.columns)

    if not (has_new or has_old):
        raise ValueError(
            "Quiz file columns not recognized. Expected either:\n"
            "Format A: Question ID, Question Text, Option 1..5, Correct Answer\n"
            "OR Format B: Question, Option A..E, Answer"
        )

    if has_new:
        qid_col = "Question ID" if "Question ID" in df.columns else None
        qtext_col = "Question Text"
        ans_col = "Correct Answer"
        option_cols = [c for c in ["Option 1", "Option 2", "Option 3", "Option 4", "Option 5"] if c in df.columns]
        if len(option_cols) < 2:
            raise ValueError("Need at least Option 1 and Option 2 columns in the quiz file.")
    else:
        qid_col = None
        qtext_col = "Question"
        ans_col = "Answer"
        option_cols = [c for c in ["Option A", "Option B", "Option C", "Option D", "Option E"] if c in df.columns]
        if len(option_cols) < 2:
            raise ValueError("Need at least Option A and Option B columns in the quiz file.")

    letter_to_index = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}

    questions = {}
    for _, row in df.iterrows():
        # Build display question with Question ID
        qtext_raw = row.get(qtext_col, None)
        if pd.isna(qtext_raw):
            continue
        qtext = str(qtext_raw).strip()
        if not qtext or qtext.lower() == "nan":
            continue

        q_display = qtext
        if qid_col:
            qid_raw = row.get(qid_col, None)
            if pd.notna(qid_raw):
                qid = str(qid_raw).strip()
                if qid and qid.lower() != "nan":
                    q_display = f"[{qid}] {qtext}"

        # Collect options
        options = []
        for c in option_cols:
            v = row.get(c, None)
            if pd.notna(v):
                s = str(v).strip()
                if s and s.lower() != "nan":
                    options.append(s)

        if len(options) < 2:
            continue

        ans_raw = row.get(ans_col, None)
        if pd.isna(ans_raw):
            continue
        ans = str(ans_raw).strip()
        if not ans:
            continue

        correct_answer = None

        # 1) A-E
        a_up = ans.upper()
        if a_up in letter_to_index:
            idx = letter_to_index[a_up]
            if idx < len(options):
                correct_answer = options[idx]

        # 2) 1-5
        if correct_answer is None:
            try:
                idx = int(float(ans)) - 1
                if 0 <= idx < len(options):
                    correct_answer = options[idx]
            except Exception:
                pass

        # 3) Exact option text
        if correct_answer is None:
            normalized = {o.strip().lower(): o for o in options}
            key = ans.strip().lower()
            if key in normalized:
                correct_answer = normalized[key]

        if correct_answer is None:
            continue

        alternatives = [correct_answer] + [o for o in options if o != correct_answer]
        questions[q_display] = alternatives

    return questions



# -----------------------------
# Quiz state
# -----------------------------
def clear_quiz_state():
    for k in [
        "quiz_questions",
        "current_question",
        "score",
        "correct_answer",
        "feedback",
        "chapter_key",
        "current_options",
    ]:
        session.pop(k, None)


def quiz_started_for(chapter_key: str) -> bool:
    chapter_key = norm_id(chapter_key)
    return (
        session.get("chapter_key") == chapter_key
        and isinstance(session.get("quiz_questions"), list)
        and len(session.get("quiz_questions")) > 0
        and isinstance(session.get("current_question"), int)
    )


def start_quiz_for_chapter(chapter, num_questions: str, randomize: str):
    clear_quiz_state()

    questions = load_questions_from_file(chapter["quiz_file"])
    if not questions:
        abort(400, description="No questions found in this chapter quiz file.")

    all_q = list(questions.items())  # [(question, [correct,...]), ...]

    if num_questions == "all":
        n = len(all_q)
    else:
        try:
            n = int(num_questions)
        except Exception:
            n = 20
        n = max(1, min(n, len(all_q)))

    if randomize == "yes":
        quiz_questions = random.sample(all_q, k=n)
    else:
        quiz_questions = all_q[:n]

    session["chapter_key"] = chapter["key"]
    session["quiz_questions"] = quiz_questions
    session["current_question"] = 0
    session["score"] = 0


def render_current_question(chapter):
    current = session.get("current_question", 0)
    quiz_questions = session.get("quiz_questions", [])

    if current >= len(quiz_questions):
        return redirect(url_for("chapter_results", chapter_key=chapter["key"]))

    question, answers = quiz_questions[current]
    correct_answer = answers[0]

    feedback = session.get("feedback")

    # keep same option order after submit
    if feedback and feedback.get("show"):
        options = session.get("current_options") or answers
    else:
        options = random.sample(answers, len(answers))
        session["current_options"] = options

    session["correct_answer"] = correct_answer

    return render_template(
        "quiz.html",
        chapter=chapter,
        active="quiz",
        question=question,
        options=options,
        current=current + 1,
        total=len(quiz_questions),
        feedback=feedback,
    )


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def chapters_home():
    # Landing page uses EXAM blocks
    exams = build_exams()
    return render_template("chapters.html", exams=exams)


@app.route("/chapter/<chapter_key>/")
def chapter_hub(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)
    return render_template("chapter_hub.html", chapter=chapter, active="hub")


@app.route("/chapter/<chapter_key>/quiz", methods=["GET", "POST"])
def chapter_quiz(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if request.method == "POST":
        num_questions = request.form.get("num_questions", "20")
        randomize = request.form.get("randomize", "yes")
        start_quiz_for_chapter(chapter, num_questions=num_questions, randomize=randomize)
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    if not quiz_started_for(chapter["key"]):
        total = len(load_questions_from_file(chapter["quiz_file"]))
        return render_template("quiz_start.html", chapter=chapter, active="quiz", total_questions=total)

    return render_current_question(chapter)


@app.route("/chapter/<chapter_key>/submit", methods=["POST"])
def chapter_submit_answer(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if not quiz_started_for(chapter["key"]):
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    selected = request.form.get("answer")
    correct = session.get("correct_answer")

    is_correct = (selected == correct)
    if is_correct:
        session["score"] = session.get("score", 0) + 1

    session["feedback"] = {
        "selected": selected,
        "correct": correct,
        "is_correct": is_correct,
        "show": True,
    }

    return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))


@app.route("/chapter/<chapter_key>/next")
def chapter_next_question(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if not quiz_started_for(chapter["key"]):
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    session["current_question"] = session.get("current_question", 0) + 1
    session.pop("feedback", None)
    session.pop("current_options", None)
    session.pop("correct_answer", None)
    return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))


@app.route("/chapter/<chapter_key>/results")
def chapter_results(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if not quiz_started_for(chapter["key"]):
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    score = session.get("score", 0)
    total = len(session.get("quiz_questions", []))
    percentage = (score / total) * 100 if total else 0.0

    return render_template(
        "results.html",
        chapter=chapter,
        active="quiz",
        score=score,
        total=total,
        percentage=percentage,
    )


@app.route("/chapter/<chapter_key>/reset")
def chapter_reset(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)
    clear_quiz_state()
    return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))


@app.route("/chapter/<chapter_key>/flashcards")
def chapter_flashcards(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    cards = []
    fc_path = chapter.get("flashcards_file", "")

    if fc_path and os.path.exists(fc_path):
        df = load_table_any(fc_path)
        df.columns = df.columns.astype(str).str.strip()

        if "Front" in df.columns and "Back" in df.columns:
            for _, r in df.iterrows():
                front = r.get("Front", "")
                back = r.get("Back", "")
                if pd.isna(front) or pd.isna(back):
                    continue
                front_s = str(front).strip()
                back_s = str(back).strip()
                if front_s and front_s.lower() != "nan":
                    cards.append({"front": front_s, "back": back_s})
        else:
            # If you want flashcards CSV to be quiz-format too, you can derive from Question/Answer:
            if "Question" in df.columns and "Answer" in df.columns:
                for _, r in df.iterrows():
                    q = r.get("Question", "")
                    a = r.get("Answer", "")
                    if pd.isna(q) or pd.isna(a):
                        continue
                    q = str(q).strip()
                    a = str(a).strip()
                    if q and q.lower() != "nan":
                        cards.append({"front": q, "back": a})
            else:
                abort(400, description="Flashcards CSV must have Front/Back or Question/Answer columns.")
    else:
        # fallback: derive from quiz correct answers
        qdict = load_questions_from_file(chapter["quiz_file"])
        cards = [{"front": q, "back": answers[0]} for q, answers in qdict.items() if answers]

    return render_template("flashcards.html", chapter=chapter, active="flashcards", cards=cards)


@app.route("/chapter/<chapter_key>/datatable")
def chapter_datatable(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    dt_path = chapter.get("datatable_file", "")
    if dt_path and os.path.exists(dt_path):
        df = load_table_any(dt_path)
    else:
        # fallback: show quiz file as table
        df = load_table_any(chapter["quiz_file"])

    columns = list(df.columns)
    rows = df.fillna("").astype(str).to_dict("records")

    return render_template("datatable.html", chapter=chapter, active="datatable", columns=columns, rows=rows)


@app.route("/chapter/<chapter_key>/resources")
def chapter_resources(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    content = ""
    rp = chapter.get("resources_file", "")
    if rp and os.path.exists(rp):
        with open(rp, "r", encoding="utf-8") as f:
            content = f.read()

    return render_template("resources.html", chapter=chapter, active="resources", content=content)


if __name__ == "__main__":
    app.run(debug=True)

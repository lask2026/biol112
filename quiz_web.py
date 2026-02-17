from flask import Flask, render_template, request, session, redirect, url_for, abort
from flask_session import Session
import pandas as pd
import random
import os
import re
import secrets

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Registry file
CHAPTERS_REGISTRY = os.path.join(BASE_DIR, "chapters.csv")


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

# Server-side sessions
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = os.path.join(BASE_DIR, "flask_session")
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = env_bool("SESSION_COOKIE_SECURE", False)
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
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"chapters.csv missing columns: {missing}")

    # Strip whitespace
    for c in ["exam_id", "exam_title", "chapter_id", "chapter_title", "quiz_file"]:
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


def safe_load_questions(path: str):
    try:
        return load_questions_from_file(path), None
    except Exception:
        return None, f"Could not load quiz file: {path}"


def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def require_csrf():
    expected = session.get("_csrf_token")
    provided = request.form.get("csrf_token", "")
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        abort(400, description="Invalid CSRF token.")


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": get_csrf_token()}


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

    Optional:
      Image column (local static path or URL), e.g. static/images/q1.png

    Returned dict key is the DISPLAY question string (includes Question ID if available).
    Value shape:
      {
        "alternatives": [correct, wrong1, ...],
        "image": "static/images/q1.png" | "https://..." | None
      }
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
        image_val = row.get("Image", None) if "Image" in df.columns else None
        image = None
        if pd.notna(image_val):
            image_s = str(image_val).strip()
            if image_s and image_s.lower() != "nan":
                image = image_s
        questions[q_display] = {
            "alternatives": alternatives,
            "image": image,
        }

    return questions



# -----------------------------
# Quiz state
# -----------------------------
def clear_quiz_state():
    for k in [
        "quiz_questions",
        "current_question",
        "score",
        "xp",
        "current_streak",
        "best_streak",
        "question_stats",
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


def start_quiz_for_chapter(chapter):
    clear_quiz_state()

    questions = load_questions_from_file(chapter["quiz_file"])
    if not questions:
        abort(400, description="No questions found in this chapter quiz file.")

    all_q = list(questions.items())  # [(question, [correct,...]), ...]

    quiz_questions = all_q

    session["chapter_key"] = chapter["key"]
    session["quiz_questions"] = quiz_questions
    session["current_question"] = 0
    session["score"] = 0
    session["xp"] = 0
    session["current_streak"] = 0
    session["best_streak"] = 0
    session["question_stats"] = {}


def render_current_question(chapter):
    current = session.get("current_question", 0)
    quiz_questions = session.get("quiz_questions", [])

    if current >= len(quiz_questions):
        return redirect(url_for("chapter_results", chapter_key=chapter["key"]))

    question, payload = quiz_questions[current]
    if isinstance(payload, dict):
        answers = payload.get("alternatives", [])
        question_image = payload.get("image")
    else:
        answers = payload
        question_image = None
    correct_answer = answers[0]

    feedback = session.get("feedback")

    # keep same option order after submit
    if feedback and feedback.get("show"):
        options = session.get("current_options") or answers
    else:
        options = random.sample(answers, len(answers))
        session["current_options"] = options

    session["correct_answer"] = correct_answer

    image_src = None
    if question_image:
        qimg = question_image.strip()
        if qimg:
            if qimg.startswith("http://") or qimg.startswith("https://"):
                image_src = qimg
            elif qimg.startswith("/static/"):
                image_src = qimg
            elif qimg.startswith("static/"):
                image_src = url_for("static", filename=qimg[len("static/"):].lstrip("/"))
            else:
                image_src = url_for("static", filename=qimg.lstrip("/"))

    return render_template(
        "quiz.html",
        chapter=chapter,
        active="quiz",
        question=question,
        question_image=image_src,
        options=options,
        current=current + 1,
        total=len(quiz_questions),
        progress_pct=((current + 1) / len(quiz_questions) * 100) if quiz_questions else 0,
        xp=session.get("xp", 0),
        current_streak=session.get("current_streak", 0),
        best_streak=session.get("best_streak", 0),
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


@app.route("/chapter/<chapter_key>/quiz", methods=["GET", "POST"])
def chapter_quiz(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if request.method == "POST":
        require_csrf()
        try:
            start_quiz_for_chapter(chapter)
        except Exception as e:
            total = 0
            return render_template(
                "quiz_start.html",
                chapter=chapter,
                active="quiz",
                total_questions=total,
                quiz_error=str(e),
            )
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    if not quiz_started_for(chapter["key"]):
        questions, qerr = safe_load_questions(chapter["quiz_file"])
        total = len(questions) if questions else 0
        return render_template(
            "quiz_start.html",
            chapter=chapter,
            active="quiz",
            total_questions=total,
            quiz_error=qerr,
        )

    return render_current_question(chapter)


@app.route("/chapter/<chapter_key>/submit", methods=["POST"])
def chapter_submit_answer(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if not quiz_started_for(chapter["key"]):
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    require_csrf()
    selected = request.form.get("answer")
    correct = session.get("correct_answer")

    is_correct = (selected == correct)
    current_idx = session.get("current_question", 0)
    question_text = ""
    quiz_questions = session.get("quiz_questions", [])
    if 0 <= current_idx < len(quiz_questions):
        question_text = quiz_questions[current_idx][0]

    question_stats = session.get("question_stats", {})
    previous = question_stats.get(str(current_idx))
    prev_correct = bool(previous and previous.get("is_correct"))

    if prev_correct and not is_correct:
        session["score"] = max(0, session.get("score", 0) - 1)
        session["xp"] = max(0, session.get("xp", 0) - 10)
    elif (not prev_correct) and is_correct:
        session["score"] = session.get("score", 0) + 1
        session["xp"] = session.get("xp", 0) + 10

    if is_correct:
        streak = session.get("current_streak", 0) + 1
        session["current_streak"] = streak
        session["best_streak"] = max(session.get("best_streak", 0), streak)
    else:
        session["current_streak"] = 0
    question_stats[str(current_idx)] = {
        "question": question_text,
        "selected": selected,
        "correct": correct,
        "is_correct": is_correct,
    }
    session["question_stats"] = question_stats

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


@app.route("/chapter/<chapter_key>/goto", methods=["POST"])
def chapter_goto_question(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)

    if not quiz_started_for(chapter["key"]):
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    require_csrf()

    total = len(session.get("quiz_questions", []))
    raw_num = (request.form.get("question_number") or "").strip()
    try:
        target = int(raw_num)
    except Exception:
        return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))

    target = max(1, min(target, total))
    session["current_question"] = target - 1
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

    question_stats = session.get("question_stats", {})
    answered_entries = list(question_stats.values())
    score = sum(1 for e in answered_entries if e.get("is_correct"))
    wrong_count = sum(1 for e in answered_entries if not e.get("is_correct"))
    answered_count = len(answered_entries)
    total = len(session.get("quiz_questions", []))
    percentage = (score / total) * 100 if total else 0.0
    unanswered_count = max(0, total - answered_count)

    wrong_questions = []
    for idx in sorted(question_stats.keys(), key=lambda x: int(x)):
        entry = question_stats[idx]
        if not entry.get("is_correct"):
            wrong_questions.append(entry)

    return render_template(
        "results.html",
        chapter=chapter,
        active="quiz",
        score=score,
        wrong_count=wrong_count,
        answered_count=answered_count,
        unanswered_count=unanswered_count,
        total=total,
        percentage=percentage,
        wrong_questions=wrong_questions,
        xp=session.get("xp", 0),
        best_streak=session.get("best_streak", 0),
        show_confetti=(answered_count > 0 and percentage >= 85),
    )


@app.route("/chapter/<chapter_key>/reset")
def chapter_reset(chapter_key):
    chapter = get_chapter_by_key(chapter_key)
    if not chapter:
        abort(404)
    clear_quiz_state()
    return redirect(url_for("chapter_quiz", chapter_key=chapter["key"]))


if __name__ == "__main__":
    app.run(debug=env_bool("FLASK_DEBUG", False))

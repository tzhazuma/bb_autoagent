"""LLM-based grading engine using OpenAI-compatible API."""

import json
import re
import time
from typing import Any

from openai import OpenAI
from pydantic import BaseModel


class GraderConfig(BaseModel):
    """Configuration for the LLM grader."""

    model: str = "gpt-4o"
    temperature: float = 0.1
    max_tokens: int = 4096
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    batch_size: int = 5
    retry_attempts: int = 3


class RubricCriterion(BaseModel):
    """A single scoring criterion within a rubric."""

    name: str
    points: float
    description: str


class Rubric(BaseModel):
    """Grading rubric with multiple criteria."""

    name: str
    description: str = ""
    criteria: list[RubricCriterion]

    @property
    def total_points(self) -> float:
        return sum(c.points for c in self.criteria)


class GradeResult(BaseModel):
    """Result of grading a single student submission."""

    student_name: str
    student_id: str
    total_score: float
    max_score: float
    criteria_scores: dict[str, float]
    feedback: str
    raw_response: str


GRADING_PROMPT_TEMPLATE = """你是一位严格且公正的课程助教，需要根据评分标准对学生答案进行评分。

## 题目
{question}

## 参考答案
{standard_answer}

## 评分标准
{rubric_text}

## 学生答案
{submission_text}

## 评分要求
请逐条对照评分标准，对学生的答案进行评分。注意：
1. 对每条评分标准给出具体的得分和理由
2. 如果学生答案部分正确，可以给部分分
3. 反馈要具体、有建设性，帮助学生改进
4. 总分不能超过评分标准的总分上限

请严格按照以下 JSON 格式输出评分结果：
```json
{{
    "total_score": <总分, 浮点数>,
    "criteria_scores": {{
        "<评分标准名称1>": <得分>,
        "<评分标准名称2>": <得分>
    }},
    "feedback": "<详细的评分反馈，使用中文，逐条说明每项标准的得分理由>"
}}
```

注意：criteria_scores 中的键名必须与评分标准中的名称完全一致。"""


class Grader:
    """LLM-powered grading engine.

    Uses OpenAI-compatible API to evaluate student submissions against
    standard answers using structured rubrics.
    """

    def __init__(self, config: GraderConfig) -> None:
        self.config = config
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
        )

    def _build_prompt(
        self,
        submission_text: str,
        question: str,
        standard_answer: str,
        rubric: Rubric,
    ) -> str:
        """Build the grading prompt with question, answer, rubric, and submission."""
        rubric_lines = []
        for i, c in enumerate(rubric.criteria, 1):
            rubric_lines.append(
                f"{i}. **{c.name}** ({c.points}分): {c.description}"
            )
        rubric_text = "\n".join(rubric_lines)

        total_reminder = (
            f"\n\n注意：评分标准的总分为 {rubric.total_points} 分，"
            f"学生得分不能超过此上限。"
        )

        prompt = GRADING_PROMPT_TEMPLATE.format(
            question=question,
            standard_answer=standard_answer,
            rubric_text=rubric_text + total_reminder,
            submission_text=submission_text,
        )
        return prompt

    def _call_llm(self, messages: list[dict[str, str]], retry: int = 0) -> str:
        """Call LLM with retry and exponential backoff."""
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            if retry >= self.config.retry_attempts:
                raise RuntimeError(
                    f"LLM call failed after {self.config.retry_attempts} retries: {e}"
                ) from e
            delay = 2 ** retry
            time.sleep(delay)
            return self._call_llm(messages, retry + 1)

    def _parse_response(self, response_text: str) -> dict[str, Any]:
        """Parse and validate LLM JSON response.

        Strips markdown code fences if present.
        """
        text = response_text.strip()
        # Strip ```json fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)

        parsed = json.loads(text)
        return parsed

    def grade(
        self,
        student_name: str,
        student_id: str,
        submission_text: str,
        question: str,
        standard_answer: str,
        rubric: Rubric,
    ) -> GradeResult:
        """Grade a single student submission."""
        system_msg = (
            "你是一个专业的课程评分助手。你必须严格按照 JSON 格式输出评分结果，"
            "不得输出任何非 JSON 内容。所有反馈使用中文。"
        )
        prompt = self._build_prompt(
            submission_text=submission_text,
            question=question,
            standard_answer=standard_answer,
            rubric=rubric,
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ]

        raw_response = self._call_llm(messages)
        parsed = self._parse_response(raw_response)

        criteria_scores: dict[str, float] = {}
        raw_criteria: dict[str, Any] = parsed.get("criteria_scores", {})
        rubric_name_map = {c.name: c.points for c in rubric.criteria}

        for name, max_points in rubric_name_map.items():
            score = raw_criteria.get(name, 0)
            criteria_scores[name] = min(float(score), max_points)

        total_score = min(
            float(parsed.get("total_score", sum(criteria_scores.values()))),
            rubric.total_points,
        )
        feedback = parsed.get("feedback", "")

        return GradeResult(
            student_name=student_name,
            student_id=student_id,
            total_score=total_score,
            max_score=rubric.total_points,
            criteria_scores=criteria_scores,
            feedback=feedback,
            raw_response=raw_response,
        )

    def grade_batch(
        self,
        submissions: list[dict[str, str]],
        question: str,
        standard_answer: str,
        rubric: Rubric,
    ) -> list[GradeResult]:
        """Grade multiple student submissions."""
        results: list[GradeResult] = []
        for sub in submissions:
            result = self.grade(
                student_name=sub.get("student_name", "Unknown"),
                student_id=sub.get("student_id", "Unknown"),
                submission_text=sub.get("submission_text", ""),
                question=question,
                standard_answer=standard_answer,
                rubric=rubric,
            )
            results.append(result)
        return results

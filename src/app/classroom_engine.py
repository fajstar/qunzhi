import random
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Union
from enum import Enum
from pydantic import BaseModel, Field, field_validator
from fastapi import FastAPI, HTTPException, Query
from contextlib import asynccontextmanager


class TeachingPhase(str, Enum):
    INTRODUCTION = "导入"
    EXPLANATION = "讲解"
    INTERACTION = "互动"
    SUMMARY = "总结"


class StudentPersonality(str, Enum):
    ACTIVE = "积极"
    INTROVERTED = "内向"
    MISCHIEVOUS = "调皮"
    SERIOUS = "认真"
    SLOW = "慢热"
    SMART = "聪明"


class StudentBase(ABC):
    def __init__(self, name: str, personality: StudentPersonality):
        self.name = name
        self.personality = personality
        self.understanding = 0.0
        self.participation = 0.0
        self.energy = 100.0
        self.attention = 100.0
        self.response_history: List[str] = []

    @abstractmethod
    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        pass

    @abstractmethod
    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        pass

    def reset(self) -> None:
        self.understanding = 0.0
        self.participation = 0.0
        self.energy = 100.0
        self.attention = 100.0
        self.response_history.clear()


class ActiveStudent(StudentBase):
    def __init__(self, name: str):
        super().__init__(name, StudentPersonality.ACTIVE)

    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        if phase == TeachingPhase.INTRODUCTION:
            return f"{self.name}（积极）: 哇！这个主题听起来太有意思了！我已经迫不及待想学了！"
        elif phase == TeachingPhase.EXPLANATION:
            return f"{self.name}（积极）: 老师，我有个问题！这里能不能再详细解释一下？"
        elif phase == TeachingPhase.INTERACTION:
            return f"{self.name}（积极）: 我来！我来！这个问题我会！"
        else:
            return f"{self.name}（积极）: 今天收获太大了！我全都理解了！"

    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        self.energy = max(0.0, self.energy - random.uniform(2.0, 5.0))
        self.attention = max(0.0, self.attention - random.uniform(1.0, 3.0))
        if phase == TeachingPhase.INTERACTION:
            self.participation = min(100.0, self.participation + random.uniform(8.0, 15.0))
        elif phase == TeachingPhase.EXPLANATION:
            self.understanding = min(100.0, self.understanding + random.uniform(5.0, 10.0))
        elif phase == TeachingPhase.SUMMARY:
            self.understanding = min(100.0, self.understanding + random.uniform(3.0, 7.0))


class IntrovertedStudent(StudentBase):
    def __init__(self, name: str):
        super().__init__(name, StudentPersonality.INTROVERTED)

    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        if phase == TeachingPhase.INTRODUCTION:
            return f"{self.name}（内向）: ……（轻轻点头，没有说话）"
        elif phase == TeachingPhase.EXPLANATION:
            return f"{self.name}（内向）: 嗯……我好像有点明白了。"
        elif phase == TeachingPhase.INTERACTION:
            return f"{self.name}（内向）: （小声）我……我可以试试吗？"
        else:
            return f"{self.name}（内向）: 谢谢老师，我会回去再复习的。"

    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        self.energy = max(0.0, self.energy - random.uniform(1.0, 3.0))
        self.attention = max(0.0, self.attention - random.uniform(0.5, 2.0))
        if phase == TeachingPhase.INTERACTION:
            self.participation = min(100.0, self.participation + random.uniform(3.0, 8.0))
        elif phase == TeachingPhase.EXPLANATION:
            self.understanding = min(100.0, self.understanding + random.uniform(4.0, 8.0))
        elif phase == TeachingPhase.SUMMARY:
            self.understanding = min(100.0, self.understanding + random.uniform(2.0, 5.0))


class MischievousStudent(StudentBase):
    def __init__(self, name: str):
        super().__init__(name, StudentPersonality.MISCHIEVOUS)

    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        if phase == TeachingPhase.INTRODUCTION:
            return f"{self.name}（调皮）: 老师，这节课能不能提前下课？我想去打游戏！"
        elif phase == TeachingPhase.EXPLANATION:
            return f"{self.name}（调皮）: 老师，这个知识点考试会考吗？不考我就不听了！"
        elif phase == TeachingPhase.INTERACTION:
            return f"{self.name}（调皮）: 老师，我同桌在玩手机！你看他！"
        else:
            return f"{self.name}（调皮）: 终于下课了！老师再见！"

    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        self.energy = max(0.0, self.energy - random.uniform(3.0, 6.0))
        self.attention = max(0.0, self.attention - random.uniform(4.0, 8.0))
        if phase == TeachingPhase.INTERACTION:
            self.participation = min(100.0, self.participation + random.uniform(2.0, 5.0))
        elif phase == TeachingPhase.EXPLANATION:
            self.understanding = min(100.0, self.understanding + random.uniform(2.0, 5.0))
        elif phase == TeachingPhase.SUMMARY:
            self.understanding = min(100.0, self.understanding + random.uniform(1.0, 3.0))


class SeriousStudent(StudentBase):
    def __init__(self, name: str):
        super().__init__(name, StudentPersonality.SERIOUS)

    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        if phase == TeachingPhase.INTRODUCTION:
            return f"{self.name}（认真）: 好的，我会认真记录这节课的重点。"
        elif phase == TeachingPhase.EXPLANATION:
            return f"{self.name}（认真）: 老师，您刚才说的第三点，能再重复一遍吗？我想记下来。"
        elif phase == TeachingPhase.INTERACTION:
            return f"{self.name}（认真）: 我认为这个问题的答案是……（详细分析）"
        else:
            return f"{self.name}（认真）: 我已经整理了笔记，课后会继续巩固。"

    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        self.energy = max(0.0, self.energy - random.uniform(1.5, 3.5))
        self.attention = max(0.0, self.attention - random.uniform(0.8, 2.0))
        if phase == TeachingPhase.INTERACTION:
            self.participation = min(100.0, self.participation + random.uniform(5.0, 10.0))
        elif phase == TeachingPhase.EXPLANATION:
            self.understanding = min(100.0, self.understanding + random.uniform(6.0, 12.0))
        elif phase == TeachingPhase.SUMMARY:
            self.understanding = min(100.0, self.understanding + random.uniform(4.0, 8.0))


class SlowStudent(StudentBase):
    def __init__(self, name: str):
        super().__init__(name, StudentPersonality.SLOW)

    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        if phase == TeachingPhase.INTRODUCTION:
            return f"{self.name}（慢热）: 嗯……我还在适应，让我先看看。"
        elif phase == TeachingPhase.EXPLANATION:
            return f"{self.name}（慢热）: 老师，我有点跟不上，能慢一点吗？"
        elif phase == TeachingPhase.INTERACTION:
            return f"{self.name}（慢热）: 我……我还在想，再给我一点时间。"
        else:
            return f"{self.name}（慢热）: 虽然有点难，但我会努力跟上的。"

    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        self.energy = max(0.0, self.energy - random.uniform(1.0, 2.5))
        self.attention = max(0.0, self.attention - random.uniform(1.5, 3.0))
        if phase == TeachingPhase.INTERACTION:
            self.participation = min(100.0, self.participation + random.uniform(1.0, 4.0))
        elif phase == TeachingPhase.EXPLANATION:
            self.understanding = min(100.0, self.understanding + random.uniform(2.0, 5.0))
        elif phase == TeachingPhase.SUMMARY:
            self.understanding = min(100.0, self.understanding + random.uniform(1.5, 4.0))


class SmartStudent(StudentBase):
    def __init__(self, name: str):
        super().__init__(name, StudentPersonality.SMART)

    def respond(self, instruction: str, phase: TeachingPhase) -> str:
        if phase == TeachingPhase.INTRODUCTION:
            return f"{self.name}（聪明）: 这个主题我提前预习过了，感觉很有意思。"
        elif phase == TeachingPhase.EXPLANATION:
            return f"{self.name}（聪明）: 老师，您讲的这个知识点，是不是和之前学的XX有关联？"
        elif phase == TeachingPhase.INTERACTION:
            return f"{self.name}（聪明）: 我有三种解法，第一种是……第二种是……第三种是……"
        else:
            return f"{self.name}（聪明）: 我已经完全掌握了，并且能举一反三。"

    def update_state(self, phase: TeachingPhase, instruction: str) -> None:
        self.energy = max(0.0, self.energy - random.uniform(1.0, 2.0))
        self.attention = max(0.0, self.attention - random.uniform(0.5, 1.5))
        if phase == TeachingPhase.INTERACTION:
            self.participation = min(100.0, self.participation + random.uniform(6.0, 12.0))
        elif phase == TeachingPhase.EXPLANATION:
            self.understanding = min(100.0, self.understanding + random.uniform(8.0, 15.0))
        elif phase == TeachingPhase.SUMMARY:
            self.understanding = min(100.0, self.understanding + random.uniform(5.0, 10.0))


class ClassroomSimulator:
    def __init__(self) -> None:
        self.students: List[StudentBase] = [
            ActiveStudent("小明"),
            IntrovertedStudent("小红"),
            MischievousStudent("小刚"),
            SeriousStudent("小丽"),
            SlowStudent("小华"),
            SmartStudent("小智"),
        ]
        self.current_phase: Optional[TeachingPhase] = None
        self.instruction_history: List[Dict[str, Any]] = []
        self.classroom_energy = 100.0
        self.classroom_attention = 100.0

    def reset(self) -> None:
        for student in self.students:
            student.reset()
        self.current_phase = None
        self.instruction_history.clear()
        self.classroom_energy = 100.0
        self.classroom_attention = 100.0

    def set_phase(self, phase: TeachingPhase) -> None:
        self.current_phase = phase

    def process_instruction(self, instruction: str, phase: TeachingPhase) -> Dict[str, Any]:
        if not instruction or not instruction.strip():
            raise ValueError("教学指令不能为空")
        if not isinstance(phase, TeachingPhase):
            raise ValueError("无效的教学环节")

        self.set_phase(phase)
        responses: List[Dict[str, str]] = []
        total_understanding = 0.0
        total_participation = 0.0

        for student in self.students:
            student.update_state(phase, instruction)
            response = student.respond(instruction, phase)
            student.response_history.append(response)
            responses.append({
                "name": student.name,
                "personality": student.personality.value,
                "response": response,
                "understanding": round(student.understanding, 2),
                "participation": round(student.participation, 2),
                "energy": round(student.energy, 2),
                "attention": round(student.attention, 2),
            })
            total_understanding += student.understanding
            total_participation += student.participation

        avg_understanding = total_understanding / len(self.students)
        avg_participation = total_participation / len(self.students)

        self.classroom_energy = sum(s.energy for s in self.students) / len(self.students)
        self.classroom_attention = sum(s.attention for s in self.students) / len(self.students)

        atmosphere = self._calculate_atmosphere(avg_participation, avg_understanding)
        suggestions = self._generate_suggestions(avg_understanding, avg_participation, phase)

        result = {
            "phase": phase.value,
            "instruction": instruction,
            "responses": responses,
            "classroom_metrics": {
                "avg_understanding": round(avg_understanding, 2),
                "avg_participation": round(avg_participation, 2),
                "classroom_energy": round(self.classroom_energy, 2),
                "classroom_attention": round(self.classroom_attention, 2),
                "atmosphere": atmosphere,
            },
            "suggestions": suggestions,
            "timestamp": time.time(),
        }

        self.instruction_history.append(result)
        return result

    def _calculate_atmosphere(self, participation: float, understanding: float) -> str:
        score = (participation * 0.6 + understanding * 0.4)
        if score >= 80:
            return "非常活跃"
        elif score >= 60:
            return "较为活跃"
        elif score >= 40:
            return "一般"
        elif score >= 20:
            return "沉闷"
        else:
            return "非常沉闷"

    def _generate_suggestions(self, understanding: float, participation: float, phase: TeachingPhase) -> List[str]:
        suggestions = []
        if understanding < 50:
            suggestions.append("建议放慢讲解速度，增加更多实例说明")
        if participation < 40:
            suggestions.append("建议增加互动环节，鼓励更多学生参与")
        if phase == TeachingPhase.EXPLANATION and understanding < 60:
            suggestions.append("建议在讲解后增加即时小测验检验理解")
        if phase == TeachingPhase.INTERACTION and participation < 50:
            suggestions.append("建议设计更有趣的互动形式，提高参与度")
        if self.classroom_attention < 60:
            suggestions.append("建议适当休息或调整教学节奏，提升注意力")
        if not suggestions:
            suggestions.append("当前教学效果良好，继续保持")
        return suggestions

    def get_student_states(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": s.name,
                "personality": s.personality.value,
                "understanding": round(s.understanding, 2),
                "participation": round(s.participation, 2),
                "energy": round(s.energy, 2),
                "attention": round(s.attention, 2),
            }
            for s in self.students
        ]

    def get_history(self) -> List[Dict[str, Any]]:
        return self.instruction_history


class InstructionRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=500, description="教学指令")
    phase: TeachingPhase = Field(..., description="教学环节")

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("教学指令不能为空")
        return v


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    students_count: int = 6


simulator = ClassroomSimulator()
app = FastAPI(title="课堂模拟器引擎", version="1.0.0")


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse()


@app.post("/api/teach")
async def teach(request: InstructionRequest) -> Dict[str, Any]:
    try:
        result = simulator.process_instruction(request.instruction, request.phase)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"内部错误: {str(e)}")


@app.get("/api/students")
async def get_students() -> List[Dict[str, Any]]:
    return simulator.get_student_states()


@app.get("/api/history")
async def get_history() -> List[Dict[str, Any]]:
    return simulator.get_history()


@app.post("/api/reset")
async def reset_simulator() -> Dict[str, str]:
    simulator.reset()
    return {"status": "reset_success"}


@app.get("/api/classroom/status")
async def get_classroom_status() -> Dict[str, Any]:
    return {
        "classroom_energy": round(simulator.classroom_energy, 2),
        "classroom_attention": round(simulator.classroom_attention, 2),
        "current_phase": simulator.current_phase.value if simulator.current_phase else None,
        "instruction_count": len(simulator.instruction_history),
    }
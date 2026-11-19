# cascade-fix

리뷰 지적을 작업자 둘이 독립으로 고치고 기계 검증·합의로 채택하는 Claude Code 스킬(공개, PolyForm Noncommercial). 스킬 본문은 `SKILL.md`, 오케스트레이션 코드는 `cascade/`(Python 3.11+, 외부 패키지 없음), 측정·설계 기록은 `docs/`에 있다.

- 시험: `python3 -m unittest discover -s tests`(3분 넘게 걸린다). 끝내기 전에 돌린다.
- `SKILL.md`의 `description`과 `when_to_use`는 합쳐서 1,536자 안으로 둔다. 넘는 부분은 스킬 목록에서 잘려 모델이 보지 못한다(공식 문서).
- 동작을 바꾸면 `README.md`와 `SKILL.md`의 설명을 함께 고치고, 릴리스할 때 버전을 올린다.

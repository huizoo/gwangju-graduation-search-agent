Python 주석(Comment) 및 Docstring 작성 참고 자료
Python 코드에서 주석과 Docstring을 활용해 함수의 역할, 매개변수, 반환값, 내부 처리 로직을 설명하는 방법을 참고할 수 있는 GitHub 코드입니다.

카카오 – khaiii
https://github.com/kakao/khaiii/blob/master/train/extract_preanal.py

코드 특징  (현 깃허브 코드가 중요한 것이 아닙니다. 대략적인 용어 설명을 위한 특징 작성입니다,)

• 함수 및 클래스별 Docstring 작성.
• Args:를 이용한 매개변수 설명.
• Returns:를 이용한 반환값 설명.
• # 주석을 이용한 내부 처리 로직 설명.
• 한국어로 함수의 역할과 조건별 처리 이유 설명.

실제 Docstring 작성 예시
```
def _filter_no_ambig(min_freq: int) -> Dict[str, str]:
    """
    문맥과 상관 없이 중의성이 없는 엔트리를 출력
    Args:
        min_freq:  최소 빈도
    Returns:
        중의성이 있는 엔트리가 제거된 사전
    """
```
주석과 Docstring의 차이

• # 주석: 코드 내부의 특정 동작이나 조건을 설명.
• """ """ Docstring: 함수·클래스의 역할과 사용 방법을 설명.
• Args: 입력 매개변수 설명.
• Returns: 반환 데이터 설명.
• Raises: 발생 가능한 예외 설명.
• Examples: 함수 사용 예시.

권장 작성 방식

1. 함수 정의 바로 아래에 Docstring 작성.
2. 함수의 목적, 입력값, 반환값 명시.
3. 복잡한 조건문이나 알고리즘에는 # 주석 추가.
4. 단순히 코드를 번역하기보다 해당 로직이 필요한 이유 설명.
5. 필요한 경우 사용 예시와 예외 처리 내용 추가.

카카오 extract_preanal.py는 함수별 Docstring과 일반 주석을 함께 활용한 Python 코드 문서화 사례입니다.
한국어로 함수의 역할, 입력값, 반환값 및 내부 처리 로직을 설명하고 있어 Python 주석 및 Docstring 작성 실습 참고 자료로 활용하기 좋습니다.
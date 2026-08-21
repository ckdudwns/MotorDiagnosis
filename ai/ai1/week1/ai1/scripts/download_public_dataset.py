"""
공개 베어링/산업 음향 이상탐지 데이터셋 안내 스크립트 (AI-1)

실제 다운로드 URL은 각 데이터셋 제공 기관 정책에 따라 다르므로,
이 스크립트는 자동 다운로드 대신 데이터셋 정보와 로컬 배치 규칙을 안내한다.
"""

DATASETS = [
    {
        "name": "CWRU Bearing Dataset",
        "description": "Case Western Reserve University에서 제공하는 베어링 결함 진동 신호 데이터셋. "
        "정상/내륜결함/외륜결함/볼결함 등 라벨 포함.",
        "info_url": "https://engineering.case.edu/bearingdatacenter",
        "expected_local_path": "data/external/cwru/",
    },
    {
        "name": "MIMII Dataset",
        "description": "산업 기계(펌프, 팬, 밸브, 슬라이드 레일)의 정상/이상 음향 데이터셋. "
        "Bind Edge AI의 모터/펌프/발전기 이상음 라벨 설계에 참고 가능.",
        "info_url": "https://zenodo.org/record/3384388",
        "expected_local_path": "data/external/mimii/",
    },
    {
        "name": "MAFAULDA",
        "description": "회전기계 결함(불균형, 축정렬불량, 베어링 결함) 진동/음향 데이터셋.",
        "info_url": "http://www02.smt.ufrj.br/~offshore/mfs/page_01.html",
        "expected_local_path": "data/external/mafaulda/",
    },
]


def list_datasets():
    print("=== AI-1 참고용 공개 데이터셋 목록 ===\n")
    for ds in DATASETS:
        print(f"- {ds['name']}")
        print(f"  설명: {ds['description']}")
        print(f"  안내 URL: {ds['info_url']}")
        print(f"  권장 로컬 경로: {ds['expected_local_path']}")
        print()
    print(
        "주의: 각 데이터셋의 라이선스/이용약관을 확인한 뒤 수동으로 다운로드해 "
        "위 '권장 로컬 경로'에 배치하세요. (자동 다운로드는 배포 정책상 지원하지 않음)"
    )


if __name__ == "__main__":
    list_datasets()

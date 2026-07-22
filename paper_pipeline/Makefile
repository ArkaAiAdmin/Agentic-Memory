.PHONY: all pdf pdf-double-blind artifact artifact-double-blind clean

MD_SINGLE := "Conflict-Free Knowledge Graph Projection.md"
PDF_SINGLE := "Conflict-Free Knowledge Graph Projection.pdf"

MD_DOUBLE := "paper_double_blind.md"
PDF_DOUBLE := "paper_double_blind.pdf"

ARTIFACT_SINGLE_ZIP := "paper_pipeline_artifact_single_blind.zip"
ARTIFACT_DOUBLE_ZIP := "paper_pipeline_artifact_double_blind.zip"

all: pdf pdf-double-blind artifact artifact-double-blind

pdf:
	pandoc $(MD_SINGLE) \
		--pdf-engine=xelatex \
		-V geometry:margin=1in \
		-V fontsize=11pt \
		-V colorlinks=true \
		-o $(PDF_SINGLE)

pdf-double-blind:
	pandoc $(MD_DOUBLE) \
		--pdf-engine=xelatex \
		-V geometry:margin=1in \
		-V fontsize=11pt \
		-V colorlinks=true \
		-o $(PDF_DOUBLE)

artifact:
	zip -r $(ARTIFACT_SINGLE_ZIP) crdt_projection.py ck_crdt.py test_pipeline.py test_adversarial.py benchmark.py README.md CITATION.cff

artifact-double-blind:
	mkdir -p .tmp_db && cp crdt_projection.py ck_crdt.py test_pipeline.py test_adversarial.py benchmark.py .tmp_db/ && cp README_double_blind.md .tmp_db/README.md
	cd .tmp_db && zip -r ../$(ARTIFACT_DOUBLE_ZIP) crdt_projection.py ck_crdt.py test_pipeline.py test_adversarial.py benchmark.py README.md
	rm -rf .tmp_db

clean:
	rm -f $(PDF_SINGLE) $(PDF_DOUBLE) $(ARTIFACT_SINGLE_ZIP) $(ARTIFACT_DOUBLE_ZIP)

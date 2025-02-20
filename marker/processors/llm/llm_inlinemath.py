import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Annotated

from pydantic import BaseModel
from tqdm import tqdm

from marker.processors.llm import BaseLLMComplexBlockProcessor

from marker.processors.util import text_to_spans
from marker.schema import BlockTypes
from marker.schema.blocks import Block
from marker.schema.document import Document
from marker.schema.groups import PageGroup
from marker.schema.registry import get_block_class


class LLMInlineMathProcessor(BaseLLMComplexBlockProcessor):
    redo_inline_math: Annotated[
        bool,
        "If True, the inline math will be re-done, otherwise it will be left as is."
    ] = False

    block_types = (BlockTypes.TextInlineMath,)
    text_math_rewriting_prompt = """You are a text correction expert specializing in accurately reproducing text from images.
You will receive an image of a text block and a set of extracted lines corresponding to the text in the image.
Your task is to correct any errors in the extracted lines, including math, formatting, and other inaccuracies, and output the corrected lines in a JSON format.
The number of output lines MUST match the number of input lines.  There are {input_line_count} input lines. Stay as faithful to the original text as possible.

**Instructions:**

1. Carefully examine the provided text block image .
2. Analyze the extracted lines.
3. For each extracted line, compare it to the corresponding line in the image.
4. Correct any errors in the extracted line, including:
    * Inline math: Ensure all mathematical expressions are correctly formatted and rendered.  Surround them with <math>...</math> tags.
    * Formatting: Maintain consistent formatting with the text block image, including spacing, indentation, and special characters.
    * Other inaccuracies:  If the image is handwritten then you may correct any spelling errors, or other discrepancies.
5. Do not remove any formatting i.e bold, italics, math, superscripts, subscripts, etc from the extracted lines unless it is necessary to correct an error.
6. Ensure that inline math is properly with inline math tags, like <math>...</math>.
7. The number of corrected lines in the output MUST equal the number of extracted lines provided in the input. Do not add or remove lines.
8. Output the corrected lines in JSON format, as shown in the example below.  Each line should be in HTML format. Only use the math, br, a, i, b, sup, sub, and span tags.
9. You absolutely cannot remove any <a href='#...'>...</a> tags, those are extremely important for references and are coming directly from the document, you MUST always preserve them.

**Example:**

Input:
```
{
 "extracted_lines": [
  "Adversarial training (AT) <a href='#page-9-1'>[23]</a>, which aims to minimize\n",
  "the model's risk under the worst-case perturbations, is cur-\n",
  "rently the most effective approach for improving the robust-\n",
  "ness of deep neural networks. For a given neural network\n",
  "f(x, w) with parameters w, the optimization objective of\n",
  "AT can be formulated as follows:\n"
 ]
}
```

Output:

```json
{
 "corrected_lines": [
  "Adversarial training (AT) <a href='#page-9-1'>[23]</a>, which aims to minimize\n",
  "the model's risk under the worst-case perturbations, is cur-\n",
  "rently the most effective approach for improving the robust-\n",
  "ness of deep neural networks. For a given neural network\n",
  "<math>f(x, w)</math> with parameters <math>w</math>, the optimization objective of\n",
  "AT can be formulated as follows:\n"
 ]
}
```

**Input:**
```json
{extracted_lines}
```
"""

    def rewrite_blocks(self, document: Document):
        if not self.redo_inline_math:
            return

        inline_blocks = [
            (page, block)
            for page in document.pages
            for block in page.contained_blocks(document, self.block_types)
        ]
        detected_blocks = [
            (page, block)
            for page in document.pages
            for block in page.contained_blocks(document, (BlockTypes.Text, BlockTypes.Caption, BlockTypes.SectionHeader))
            if any([b.formats and "math" in b.formats for b in block.contained_blocks(document, (BlockTypes.Line,))])
        ]
        inference_blocks = inline_blocks + detected_blocks
        # Don't show progress if there are no blocks to process
        total_blocks = len(inference_blocks)
        if total_blocks == 0:
            return

        pbar = tqdm(desc=f"{self.__class__.__name__} running", disable=self.disable_tqdm)
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as executor:
            for future in as_completed([
                executor.submit(self.process_rewriting, document, b[0], b[1])
                for b in inference_blocks
            ]):
                future.result()  # Raise exceptions if any occurred
                pbar.update(1)

        pbar.close()

    def get_block_lines(self, block: Block, document: Document) -> Tuple[list, list]:
        text_lines = block.contained_blocks(document, (BlockTypes.Line,))
        extracted_lines = [line.formatted_text(document) for line in text_lines]
        return text_lines, extracted_lines

    def process_rewriting(self, document: Document, page: PageGroup, block: Block):
        SpanClass = get_block_class(BlockTypes.Span)

        text_lines, extracted_lines = self.get_block_lines(block, document)
        prompt = (self.text_math_rewriting_prompt.replace("{extracted_lines}",
                                                         json.dumps({"extracted_lines": extracted_lines}, indent=2))
                                                    .replace("{input_line_count}", str(len(extracted_lines)))
                  )

        image = self.extract_image(document, block)
        response = self.llm_service(prompt, image, block, LLMTextSchema)

        if not response or "corrected_lines" not in response:
            block.update_metadata(llm_error_count=1)
            return

        corrected_lines = response["corrected_lines"]
        if not corrected_lines or len(corrected_lines) != len(extracted_lines):
            block.update_metadata(llm_error_count=1)
            return

        for text_line, corrected_text in zip(text_lines, corrected_lines):
            text_line.structure = []
            corrected_spans = text_to_spans(corrected_text)

            for span_idx, span in enumerate(corrected_spans):
                if span_idx == len(corrected_spans) - 1:
                    span['content'] += "\n"

                span_block = page.add_full_block(
                    SpanClass(
                        polygon=text_line.polygon,
                        text=span['content'],
                        font='Unknown',
                        font_weight=0,
                        font_size=0,
                        minimum_position=0,
                        maximum_position=0,
                        formats=[span['type']],
                        url=span.get('url'),
                        page_id=text_line.page_id,
                        text_extraction_method="gemini",
                    )
                )
                text_line.structure.append(span_block.id)

class LLMTextSchema(BaseModel):
    corrected_lines: List[str]
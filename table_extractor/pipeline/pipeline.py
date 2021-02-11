import json
import logging
from typing import List, Tuple, Dict, Any, Union

from pathlib import Path

import cv2
from tesserocr import PSM

from table_extractor.bordered_service.bordered_tables_detection import detect_tables_on_page
from table_extractor.bordered_service.models import InferenceTable, Page
from table_extractor.cascade_rcnn_service.inference import CascadeRCNNInferenceService
from table_extractor.headers.header_utils import HeaderChecker
from table_extractor.inference_table_service.constuct_table_from_inference import construct_table_from_cells, \
    find_grid_table, reconstruct_table_from_grid
from table_extractor.model.table import StructuredTable, TextField, Cell, Table, BorderBox, CellLinked, \
    StructuredTableHeadered
from table_extractor.paddle_service.text_detector import PaddleDetector
from table_extractor.pdf_service.pdf_to_image import convert_pdf_to_images
from table_extractor.poppler_service.poppler_text_extractor import extract_text, \
    poppler_text_field_to_text_field, PopplerPage
from table_extractor.borderless_service.semi_bordered import semi_bordered
from table_extractor.tesseract_service.tesseract_extractor import TextExtractor
from table_extractor.text_cells_matcher.text_cells_matcher import match_table_text, match_cells_text_fields
from table_extractor.visualization.table_visualizer import TableVisualizer

logger = logging.getLogger(__name__)


def cnt_ciphers(cells: List[Cell]):
    count = 0
    all_chars_count = 0
    for cell in cells:
        sentence = "".join([tb.text for tb in cell.text_boxes])
        for char in sentence:
            if char in '0123456789':
                count += 1
        all_chars_count += len(sentence)
    return count / all_chars_count if all_chars_count else 0.


def actualize_header(table: StructuredTable):
    table_rows = table.rows
    count_ciphers = cnt_ciphers(table_rows[0])
    header_candidates = [table_rows[0]]
    current_ciphers = count_ciphers
    for row in table_rows[1:]:
        count = cnt_ciphers(row)
        if count > current_ciphers:
            break
        else:
            header_candidates.append(row)

    if len(header_candidates) < len(table_rows):
        return StructuredTableHeadered.from_structured_and_rows(table, header_candidates)
    return StructuredTableHeadered(
        bbox=table.bbox,
        cells=table.cells,
        header=[]
    )


def merge_text_fields(paddle_t_b: List[TextField], poppler_t_b: List[TextField]) -> List[TextField]:
    not_matched = []
    merged_t_b = []
    for pop_t_b in poppler_t_b:
        merged = False
        for pad_t_b in paddle_t_b:
            if pop_t_b.bbox.box_is_inside_another(pad_t_b.bbox, threshold=0.00):
                merged_t_b.append(TextField(
                    bbox=pad_t_b.bbox.merge(pop_t_b.bbox),
                    text=pop_t_b.text
                ))
                merged = True
        if not merged:
            not_matched.append(pop_t_b)

    for pad_t_b in paddle_t_b:
        exists = False
        for mer_t_b in merged_t_b:
            if mer_t_b.bbox.box_is_inside_another(pad_t_b.bbox, threshold=0.0):
                exists = True
        if not exists:
            not_matched.append(pad_t_b)

    merged_t_b.extend(not_matched)

    return merged_t_b


def text_to_cell(text_field: TextField):
    return Cell(
        top_left_x=text_field.bbox.top_left_x,
        top_left_y=text_field.bbox.top_left_y,
        bottom_right_x=text_field.bbox.bottom_right_x,
        bottom_right_y=text_field.bbox.bottom_right_y,
        text_boxes=[text_field]
    )


def merge_closest_text_fields(text_fields: List[TextField]):
    merged_fields: List[TextField] = []
    curr_field: TextField = None
    for text_field in sorted(text_fields, key=lambda x: (x.bbox.top_left_y, x.bbox.top_left_x)):
        if not curr_field:
            curr_field = text_field
        if curr_field:
            if 20 > text_field.bbox.top_left_x - curr_field.bbox.bottom_right_x > -20:
                curr_field = TextField(
                    bbox=curr_field.bbox.merge(text_field.bbox),
                    text=curr_field.text + " " + text_field.text
                )
            else:
                merged_fields.append(curr_field)
                curr_field = text_field
    if curr_field:
        merged_fields.append(curr_field)

    return merged_fields


def pdf_preprocess(pdf_path: Path, output_path: Path) -> Tuple[Path, Dict[str, PopplerPage]]:
    images_path = convert_pdf_to_images(pdf_path, output_path)
    poppler_pages = extract_text(pdf_path)
    return images_path, poppler_pages


def actualize_text(table: StructuredTable, image_path: Path):
    with TextExtractor(str(image_path.absolute())) as te:
        for cell in table.cells:
            if not cell.text_boxes or any([not text_box.text for text_box in cell.text_boxes]):
                text, _ = te.extract(
                    cell.top_left_x, cell.top_left_y,
                    cell.width, cell.height
                )
                cell.text_boxes.append(TextField(bbox=cell, text=text))


def semi_border_to_struct(semi_border: Table, image_shape: Tuple[int, int]) -> StructuredTable:
    cells = []
    for row in semi_border.rows:
        cells.extend(row.objs)
    structured_table = construct_table_from_cells(semi_border.bbox, cells, image_shape)
    return structured_table


def bordered_to_struct(bordered_table: Table) -> StructuredTable:
    v_lines = []
    for col in bordered_table.cols:
        v_lines.extend([col.bbox.top_left_x, col.bbox.bottom_right_x])
    v_lines = sorted(v_lines)
    v_lines_merged = [v_lines[0], v_lines[-1]]
    for i in range(0, len(v_lines[1:-1]) // 2):
        v_lines_merged.append((v_lines[2 * i] + v_lines[2 * i + 1]) // 2)
    v_lines = sorted(list(set(v_lines_merged)))

    h_lines = []
    for row in bordered_table.rows:
        h_lines.extend([row.bbox.top_left_y, row.bbox.bottom_right_y])
    h_lines = sorted(h_lines)
    h_lines_merged = [h_lines[0], h_lines[-1]]
    for i in range(0, len(h_lines[1:-1]) // 2):
        h_lines_merged.append((h_lines[2 * i] + h_lines[2 * i + 1]) // 2)
    h_lines = sorted(list(set(h_lines_merged)))
    cells = []
    for row in bordered_table.rows:
        cells.extend(row.objs)
    grid = find_grid_table(h_lines, v_lines)
    table, _ = reconstruct_table_from_grid(grid, cells)
    return table


def cell_to_dict(cell: CellLinked):
    return {
        'row': cell.row,
        'column': cell.col,
        'rowspan': cell.row_span,
        'colspan': cell.col_span,
        'bbox': {
            'left': cell.top_left_x,
            'top': cell.top_left_y,
            'height': cell.height,
            'width': cell.width
        },
        'text': " ".join([field.text for field in
                          sorted(cell.text_boxes, key=lambda x: (x.bbox.top_left_y, x.bbox.top_left_x))])
    }


def table_to_dict(table: StructuredTableHeadered):
    header = []
    for row in table.header:
        for cell in row:
            header.append(cell)
    return {
        'bbox': {
            'left': table.bbox.top_left_x,
            'top': table.bbox.top_left_y,
            'height': table.bbox.height,
            'width': table.bbox.width
        },
        'header': [cell_to_dict(cell) for cell in header],
        'cells': [cell_to_dict(cell) for cell in table.cells]
    }


def text_to_dict(text: TextField):
    return {
        'bbox': {
            'left': text.bbox.top_left_x,
            'top': text.bbox.top_left_y,
            'height': text.bbox.height,
            'width': text.bbox.width
        },
        'text': text.text
    }


def block_to_dict(block: Union[TextField, StructuredTableHeadered]):
    if isinstance(block, StructuredTableHeadered):
        return table_to_dict(block)
    if isinstance(block, TextField):
        return text_to_dict(block)
    raise TypeError(f"Incorrect type provided: {type(block)}")


def page_to_dict(page: Page):
    blocks = page.blocks
    return {
        'page_num': page.page_num,
        'bbox': {
            'left': page.bbox.top_left_x,
            'top': page.bbox.top_left_y,
            'height': page.bbox.height,
            'width': page.bbox.width
        },
        'blocks': [block_to_dict(block) for block in blocks],
    }


def save_page(page_dict: Dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path.absolute()), 'w') as f:
        f.write(json.dumps(page_dict, indent=4))


class PageProcessor:
    def __init__(self,
                 inference_service: CascadeRCNNInferenceService,
                 text_detector: PaddleDetector,
                 visualizer: TableVisualizer,
                 paddle_on=True
                 ):
        self.inference_service = inference_service
        self.text_detector = text_detector
        self.visualizer = visualizer
        self.paddle_on = paddle_on
        self.header_checker = HeaderChecker()

    def analyse(self, series: List[CellLinked]):
        # Check if series is header
        headers = []
        for cell in series:
            header_score, cell_score = self.header_checker.get_cell_score(cell)
            if header_score > cell_score:
                headers.append(cell)

        thresh = 5
        return len(headers) > (len(series) / thresh) if len(series) > thresh else len(headers) > (len(series) / 2)

    def create_header(self, series: List[List[CellLinked]], header_limit: int):
        """
        Search for headers based on cells contents
        @param series: cols or rows of the table
        @param table:
        @param header_limit:
        """
        header_candidates = []
        last_header = None
        for idx, line in enumerate(series[:header_limit]):
            if self.analyse(line):
                header_candidates.append((idx, True, line))
                last_header = idx
            else:
                header_candidates.append((idx, False, line))

        if last_header is not None:
            header = [line for idx, is_header, line in header_candidates[:last_header + 1]]
        else:
            header = []

        if len(header) > 0.75 * len(series):
            with open('cases75.txt', 'a') as f:
                f.write(str(series) + '\n')
            header = []

        return header

    def extract_table_from_inference(self,
                                     img,
                                     inf_table: InferenceTable,
                                     not_matched_text: List[TextField],
                                     image_shape: Tuple[int, int],
                                     image_path: Path) -> StructuredTable:
        merged_t_fields = merge_closest_text_fields(sorted(not_matched_text,
                                                           key=lambda x: (x.bbox.top_left_y, x.bbox.top_left_x)))

        for cell in inf_table.tags:
            if cell.text_boxes and len(cell.text_boxes) == 1:
                cell.top_left_x = cell.text_boxes[0].bbox.top_left_x
                cell.top_left_y = cell.text_boxes[0].bbox.top_left_y
                cell.bottom_right_x = cell.text_boxes[0].bbox.bottom_right_x
                cell.bottom_right_y = cell.text_boxes[0].bbox.bottom_right_y
            if cell.text_boxes and len(cell.text_boxes) > 1:
                cell.top_left_x = min([text_box.bbox.top_left_x for text_box in cell.text_boxes])
                cell.top_left_y = min([text_box.bbox.top_left_y for text_box in cell.text_boxes])
                cell.bottom_right_x = max([text_box.bbox.bottom_right_x for text_box in cell.text_boxes])
                cell.bottom_right_y = max([text_box.bbox.bottom_right_y for text_box in cell.text_boxes])

        inf_table.tags.extend([text_to_cell(text_field) for text_field in merged_t_fields])

        with TextExtractor(str(image_path.absolute())) as te:
            for text_field in inf_table.tags:
                text, conf, region = te.extract_region(
                    text_field.top_left_x, text_field.top_left_y,
                    text_field.width, text_field.height
                )
                if region:
                    regions = [[reg[1]['x'],
                                reg[1]['y'],
                                reg[1]['x'] + reg[1]['w'],
                                reg[1]['y'] + reg[1]['h']] for reg in region]
                    text_field.bottom_right_x = min(text_field.top_left_x + max([x2 for x, y, x2, y2 in regions]),
                                                    text_field.bottom_right_x)
                    text_field.bottom_right_y = min(text_field.top_left_y + max([y2 for x, y, x2, y2 in regions]),
                                                    text_field.bottom_right_y)
                    text_field.top_left_x = max(text_field.top_left_x + min([x for x, y, x2, y2 in regions]),
                                                text_field.top_left_x)
                    text_field.top_left_y = max(text_field.top_left_y + min([y for x, y, x2, y2 in regions]),
                                                text_field.top_left_y)
        self.visualizer.draw_object_and_save(img, [inf_table],
                                             image_path.parent.parent / 'modified_cells'
                                             / f"{str(image_path.name).replace('.png', '')}_"
                                               f"{inf_table.bbox.top_left_x}_{inf_table.bbox.top_left_y}.png")
        return construct_table_from_cells(inf_table.bbox, inf_table.tags, image_shape)

    def _scale_poppler_result(self, img, output_path, poppler_page, image_path):
        scale = img.shape[0] / poppler_page.bbox.height
        text_fields = [poppler_text_field_to_text_field(text_field, scale) for text_field in poppler_page.text_fields]
        if text_fields:
            self.visualizer.draw_object_and_save(img, text_fields,
                                                 Path(f"{output_path}/poppler_text/{image_path.name}"))
        return text_fields

    def process_pages(self, images_path: Path, poppler_pages: Dict[str, PopplerPage]) -> List:
        pages = []
        for image_path in sorted(images_path.glob("*.png")):
            try:
                pages.append(self.process_page(image_path,
                                               images_path.parent,
                                               poppler_pages[image_path.name.split(".")[0]]))
            except Exception as e:
                # ToDo: Rewrite, needed to not to fail pipeline for now in sequential mode
                logger.warning(str(e))
                raise e
        return pages

    def process_page(self, image_path: Path, output_path: Path, poppler_page) -> Dict[str, Any]:
        img = cv2.imread(str(image_path.absolute()))
        page = Page(
            page_num=int(image_path.name.split(".")[0]),
            bbox=BorderBox(
                top_left_x=0,
                top_left_y=0,
                bottom_right_x=img.shape[1],
                bottom_right_y=img.shape[0]
            )
        )
        text_fields = self._scale_poppler_result(img, output_path, poppler_page, image_path)

        inference_tables = self.inference_service.inference_image(image_path)
        if not inference_tables:
            return page_to_dict(page)

        has_bordered = any([i_tab.label == 'Bordered' for i_tab in inference_tables])

        self.visualizer.draw_object_and_save(
            img, inference_tables, Path(f"{output_path}/inference_result/{image_path.name}"))

        text_fields_to_match = text_fields

        semi_bordered_tables = []
        detected_tables = []
        for inf_table in inference_tables:
            in_inf_table, text_fields_to_match = match_table_text(inf_table, text_fields_to_match)
            paddle_fields = self.text_detector.extract_table_text(img, inf_table.bbox)
            if paddle_fields:
                in_inf_table = merge_text_fields(paddle_fields, in_inf_table)

            mask_rcnn_count_matches, not_matched = match_cells_text_fields(inf_table.tags, in_inf_table)

            if inf_table.label == 'Borderless':
                semi_border = semi_bordered(img, inf_table)
                if semi_border:
                    semi_bordered_tables.append(semi_border)
                    semi_border_score = match_cells_table(in_inf_table, semi_border)
                    if semi_border_score >= mask_rcnn_count_matches and semi_border.count_cells() > len(inf_table.tags):
                        struct_table = semi_border_to_struct(semi_border, img.shape)
                        if struct_table:
                            detected_tables.append((semi_border_score, struct_table))
                        continue
            struct = self.extract_table_from_inference(img, inf_table, not_matched, img.shape, image_path)
            if struct:
                detected_tables.append((mask_rcnn_count_matches, struct))

        if has_bordered or any(score < 0.2 * len(table.cells) for score, table in detected_tables):
            image = detect_tables_on_page(image_path, draw=self.visualizer.should_visualize)
            if image.tables:
                text_fields_to_match = text_fields
                for bordered_table in image.tables:
                    matched = False
                    for score, inf_table in detected_tables:
                        if inf_table.bbox.box_is_inside_another(bordered_table.bbox):
                            in_table, text_fields_to_match = match_table_text(inf_table, text_fields_to_match)
                            paddle_fields = self.text_detector.extract_table_text(img, inf_table.bbox)
                            if paddle_fields:
                                in_table = merge_text_fields(paddle_fields, in_table)

                            bordered_score = match_cells_table(in_table, bordered_table)
                            if bordered_score >= score * 0.5 \
                                    and bordered_table.count_cells() >= len(inf_table.cells) * 0.5:
                                struct_table = semi_border_to_struct(bordered_table, img.shape)
                                if struct_table:
                                    page.tables.append(struct_table)
                            else:
                                page.tables.append(inf_table)
                            detected_tables.remove((score, inf_table))
                            matched = True
                            break
                    if not matched:
                        in_table, text_fields_to_match = match_table_text(bordered_table, text_fields_to_match)
                        _ = match_cells_table(in_table, bordered_table)
                        struct_table = semi_border_to_struct(bordered_table, img.shape)
                        if struct_table:
                            page.tables.append(struct_table)
                if detected_tables:
                    page.tables.extend([inf_table for _, inf_table in detected_tables])
            else:
                page.tables.extend([tab for _, tab in detected_tables])
        else:
            page.tables.extend([tab for _, tab in detected_tables])
        for table in page.tables:
            actualize_text(table, image_path)

        # TODO: Headers should be created only once
        cell_header_scores = []
        for table in page.tables:
            cell_header_scores.extend(self.header_checker.get_cell_scores(table.cells))

        self.visualizer.draw_object_and_save(img,
                                             cell_header_scores,
                                             output_path / 'cells_header' / f"{page.page_num}.png")

        tables_with_header = []
        for table in page.tables:
            header_rows = self.create_header(table.rows, 6)
            table_with_header = StructuredTableHeadered.from_structured_and_rows(table, header_rows)
            header_cols = self.create_header(table.cols, 5)
            # TODO: Cells should be actualized only once
            table_with_header.actualize_header_with_cols(header_cols)
            tables_with_header.append(table_with_header)
        page.tables = tables_with_header

        with TextExtractor(str(image_path.absolute()), seg_mode=PSM.SPARSE_TEXT) as extractor:
            text_borders = [1]
            for table in page.tables:
                _, y, _, y2 = table.bbox.box
                text_borders.extend([y, y2])
            text_borders.append(img.shape[0])
            text_candidate_boxes: List[BorderBox] = []
            for i in range(len(text_borders) // 2):
                if text_borders[i * 2 + 1] - text_borders[i * 2] > 3:
                    text_candidate_boxes.append(
                        BorderBox(
                            top_left_x=1,
                            top_left_y=text_borders[i * 2],
                            bottom_right_x=img.shape[1],
                            bottom_right_y=text_borders[i * 2 + 1],
                        )
                    )
            for box in text_candidate_boxes:
                text, _ = extractor.extract(
                    box.top_left_x, box.top_left_y,
                    box.width, box.height
                )
                if text:
                    page.text.append(TextField(box, text))

        self.visualizer.draw_object_and_save(img,
                                             semi_bordered_tables,
                                             output_path.joinpath('semi_bordered_tables').joinpath(image_path.name))
        self.visualizer.draw_object_and_save(img,
                                             page.tables,
                                             output_path.joinpath('tables').joinpath(image_path.name))
        page_dict = page_to_dict(page)
        if self.visualizer.should_visualize:
            save_page(page_dict, output_path / 'pages' / f"{page.page_num}.json")

        return page_dict


def match_cells_table(text_fields: List[TextField], table: Table) -> int:
    table_cells = [row.objs for row in table.rows]
    cells = []
    for cell in table_cells:
        cells.extend(cell)
    score, not_matched = match_cells_text_fields(cells, text_fields)
    return score

from argparse import ArgumentParser
from time import sleep
from typing import List, Optional

import cv2
import numpy

import facefusion.jobs.job_manager
import facefusion.jobs.job_store
import facefusion.processors.core as processors
from facefusion import config, logger, process_manager, state_manager, wording
from facefusion.common_helper import create_metavar, map_float
from facefusion.content_analyser import clear_content_analyser
from facefusion.download import conditional_download, is_download_done
from facefusion.execution import create_inference_session_pool
from facefusion.face_analyser import clear_face_analyser, get_many_faces, get_one_face
from facefusion.face_helper import paste_back, warp_face_by_face_landmark_5
from facefusion.face_masker import clear_face_occluder, create_face_mask, create_occlusion_mask, create_static_box_mask
from facefusion.face_selector import find_similar_faces, sort_and_filter_faces
from facefusion.face_store import get_reference_faces
from facefusion.filesystem import in_directory, is_file, is_image, is_video, resolve_relative_path, same_file_extension
from facefusion.processors import choices as processors_choices
from facefusion.processors.typing import ExpressionRestorerInputs
from facefusion.program_helper import find_argument_group
from facefusion.thread_helper import thread_lock, thread_semaphore
from facefusion.typing import Args, Face, InferenceSessionPool, ModelOptions, ModelSet, ProcessMode, QueuePayload, UpdateProgress, VisionFrame
from facefusion.vision import get_video_frame, read_image, read_static_image, write_image

INFERENCE_SESSION_POOL : Optional[InferenceSessionPool] = None
NAME = __name__.upper()
MODEL_SET : ModelSet =\
{
	'live_portrait':
	{
		'sources':
		{
			'feature_extractor':
			{
				'url': 'https://github.com/harisreedhar/LivePortrait-Experiments/releases/download/v3/feature_extractor.onnx',
				'path': resolve_relative_path('../.assets/models/feature_extractor.onnx')
			},
			'motion_extractor':
			{
				'url': 'https://github.com/harisreedhar/LivePortrait-Experiments/releases/download/v3/motion_extractor.onnx',
				'path': resolve_relative_path('../.assets/models/motion_extractor.onnx')
			},
			'generator':
			{
				'url': 'https://github.com/harisreedhar/LivePortrait-Experiments/releases/download/v3/generator.onnx',
				'path': resolve_relative_path('../.assets/models/generator.onnx')
			}
		},
		'template': 'arcface_128_v2',
		'size': (512, 512)
	}
}


def get_inference_session_pool() -> InferenceSessionPool:
	global INFERENCE_SESSION_POOL

	with thread_lock():
		while process_manager.is_checking():
			sleep(0.5)
		if INFERENCE_SESSION_POOL is None:
			model_sources = get_model_options().get('sources')
			INFERENCE_SESSION_POOL = create_inference_session_pool(model_sources, state_manager.get_item('execution_device_id'), state_manager.get_item('execution_providers'))
	return INFERENCE_SESSION_POOL


def clear_inference_session_pool() -> None:
	global INFERENCE_SESSION_POOL

	INFERENCE_SESSION_POOL = None


def get_model_options() -> ModelOptions:
	return MODEL_SET[state_manager.get_item('expression_restorer_model')]


def register_args(program : ArgumentParser) -> None:
	group_processors = find_argument_group(program, 'processors')
	if group_processors:
		group_processors.add_argument('--expression-restorer-model', help = wording.get('help.expression_restorer_model'), default = config.get_str_value('processors.expression_restorer_model', 'live_portrait'), choices = processors_choices.expression_restorer_models)
		group_processors.add_argument('--expression-restorer-factor', help = wording.get('help.expression_restorer_factor'), type = int, default = config.get_int_value('processors.expression_restorer_factor', '100'), choices = processors_choices.expression_restorer_factor_range, metavar = create_metavar(processors_choices.expression_restorer_factor_range))
		facefusion.jobs.job_store.register_step_keys([ 'expression_restorer_model','expression_restorer_factor' ])


def apply_args(args : Args) -> None:
	state_manager.init_item('expression_restorer_model', args.get('expression_restorer_model'))
	state_manager.init_item('expression_restorer_factor', args.get('expression_restorer_factor'))


def pre_check() -> bool:
	download_directory_path = resolve_relative_path('../.assets/models')
	models = get_model_options().get('models')
	model_urls = [ models.get(model).get('url') for model in models.keys() ]
	model_paths = [ models.get(model).get('path') for model in models.keys() ]

	if not state_manager.get_item('skip_download'):
		process_manager.check()
		conditional_download(download_directory_path, model_urls)
		process_manager.end()
	return all(is_file(model_path) for model_path in model_paths)


def post_check() -> bool:
	models = get_model_options().get('models')
	model_urls = [ models.get(model).get('url') for model in models.keys() ]
	model_paths = [ models.get(model).get('path') for model in models.keys() ]

	if not state_manager.get_item('skip_download'):
		for model_url, model_path in zip(model_urls, model_paths):
			if not is_download_done(model_url, model_path):
				logger.error(wording.get('model_download_not_done') + wording.get('exclamation_mark'), NAME)
				return False
	for model_path in model_paths:
		if not is_file(model_path):
			logger.error(wording.get('model_file_not_present') + wording.get('exclamation_mark'), NAME)
			return False
	return True


def pre_process(mode : ProcessMode) -> bool:
	if mode in [ 'output', 'preview' ] and not is_image(state_manager.get_item('target_path')) and not is_video(state_manager.get_item('target_path')):
		logger.error(wording.get('choose_image_or_video_target') + wording.get('exclamation_mark'), NAME)
		return False
	if mode == 'output' and not in_directory(state_manager.get_item('output_path')):
		logger.error(wording.get('specify_image_or_video_output') + wording.get('exclamation_mark'), NAME)
		return False
	if mode == 'output' and not same_file_extension([ state_manager.get_item('target_path'), state_manager.get_item('output_path') ]):
		logger.error(wording.get('match_target_and_output_extension') + wording.get('exclamation_mark'), NAME)
		return False
	return True


def post_process() -> None:
	read_static_image.cache_clear()
	if state_manager.get_item('video_memory_strategy') in [ 'strict', 'moderate' ]:
		clear_inference_session_pool()
	if state_manager.get_item('video_memory_strategy') == 'strict':
		clear_face_analyser()
		clear_content_analyser()
		clear_face_occluder()


def restore_expression(source_vision_frame : VisionFrame, target_face: Face, temp_vision_frame : VisionFrame) -> VisionFrame:
	model_template = get_model_options().get('template')
	model_size = get_model_options().get('size')
	expression_restorer_factor = map_float(float(state_manager.get_item('expression_restorer_factor')), 0, 200, 0, 2)
	source_vision_frame = cv2.resize(source_vision_frame, temp_vision_frame.shape[:2][::-1])
	source_crop_vision_frame, _ = warp_face_by_face_landmark_5(source_vision_frame, target_face.landmark_set.get('5/68'), model_template, model_size)
	target_crop_vision_frame, affine_matrix = warp_face_by_face_landmark_5(temp_vision_frame, target_face.landmark_set.get('5/68'), model_template, model_size)
	box_mask = create_static_box_mask(target_crop_vision_frame.shape[:2][::-1], state_manager.get_item('face_mask_blur'), (0, 0, 0, 0))
	crop_masks =\
	[
		box_mask
	]

	if 'occlusion' in state_manager.get_item('face_mask_types'):
		occlusion_mask = create_occlusion_mask(target_crop_vision_frame)
		crop_masks.append(occlusion_mask)
	source_crop_vision_frame = prepare_crop_frame(source_crop_vision_frame)
	target_crop_vision_frame = prepare_crop_frame(target_crop_vision_frame)
	target_crop_vision_frame = apply_restore_expression(source_crop_vision_frame, target_crop_vision_frame, expression_restorer_factor)
	target_crop_vision_frame = normalize_crop_frame(target_crop_vision_frame)
	crop_masks.append(create_face_mask(target_crop_vision_frame))
	crop_mask = numpy.minimum.reduce(crop_masks).clip(0, 1)
	temp_vision_frame = paste_back(temp_vision_frame, target_crop_vision_frame, crop_mask, affine_matrix)
	return temp_vision_frame


def apply_restore_expression(source_crop_vision_frame : VisionFrame, target_crop_vision_frame : VisionFrame, expression_restorer_factor : float) -> VisionFrame:
	feature_extractor = get_inference_session_pool().get('feature_extractor')
	motion_extractor = get_inference_session_pool().get('motion_extractor')
	generator = get_inference_session_pool().get('generator')

	with thread_semaphore():
		feature_volume = feature_extractor.run(None,
		{
			'input': target_crop_vision_frame
		})[0]

	with thread_semaphore():
		_, _, _, source_expression, _, _ = motion_extractor.run(None,
		{
			'input': source_crop_vision_frame
		})

	with thread_semaphore():
		target_rotation, target_scale, target_translation, target_expression, target_motion_points_raw, target_motion_points = motion_extractor.run(None,
		{
			'input': target_crop_vision_frame
		})

	expression = source_expression * expression_restorer_factor + target_expression * (1 - expression_restorer_factor)
	motion_points = target_scale * (target_motion_points_raw @ target_rotation + expression) + target_translation

	with thread_semaphore():
		crop_vision_frame = generator.run(None,
		{
			'feature_3d': feature_volume,
			'kp_source': target_motion_points,
			'kp_driving': motion_points
		})[0][0]

	return crop_vision_frame


def prepare_crop_frame(crop_vision_frame : VisionFrame) -> VisionFrame:
	crop_vision_frame = cv2.resize(crop_vision_frame, (256, 256), interpolation = cv2.INTER_AREA)
	crop_vision_frame = crop_vision_frame[:, :, ::-1] / 255.0
	crop_vision_frame = numpy.expand_dims(crop_vision_frame.transpose(2, 0, 1), axis = 0).astype(numpy.float32)
	return crop_vision_frame


def normalize_crop_frame(crop_vision_frame : VisionFrame) -> VisionFrame:
	crop_vision_frame = crop_vision_frame.transpose(1, 2, 0).clip(0, 1)
	crop_vision_frame = (crop_vision_frame * 255.0)
	crop_vision_frame = crop_vision_frame.astype(numpy.uint8)[:, :, ::-1]
	return crop_vision_frame


def get_reference_frame(source_face : Face, target_face : Face, temp_vision_frame : VisionFrame) -> VisionFrame:
	pass


def process_frame(inputs : ExpressionRestorerInputs) -> VisionFrame:
	reference_faces = inputs.get('reference_faces')
	source_vision_frame = inputs.get('source_vision_frame')
	target_vision_frame = inputs.get('target_vision_frame')
	many_faces = sort_and_filter_faces(get_many_faces([ target_vision_frame ]))

	if state_manager.get_item('face_selector_mode') == 'many':
		if many_faces:
			for target_face in many_faces:
				target_vision_frame = restore_expression(source_vision_frame, target_face, target_vision_frame)
	if state_manager.get_item('face_selector_mode') == 'one':
		target_face = get_one_face(many_faces)
		if target_face:
			target_vision_frame = restore_expression(source_vision_frame, target_face, target_vision_frame)
	if state_manager.get_item('face_selector_mode') == 'reference':
		similar_faces = find_similar_faces(many_faces, reference_faces, state_manager.get_item('reference_face_distance'))
		if similar_faces:
			for similar_face in similar_faces:
				target_vision_frame = restore_expression(source_vision_frame, similar_face, target_vision_frame)
	return target_vision_frame


def process_frames(source_path : List[str], queue_payloads : List[QueuePayload], update_progress : UpdateProgress) -> None:
	reference_faces = get_reference_faces() if 'reference' in state_manager.get_item('face_selector_mode') else None

	for queue_payload in process_manager.manage(queue_payloads):
		source_vision_frame = get_video_frame(state_manager.get_item('target_path'), queue_payload['frame_number'])
		target_vision_path = queue_payload['frame_path']
		target_vision_frame = read_image(target_vision_path)
		output_vision_frame = process_frame(
		{
			'reference_faces': reference_faces,
			'source_vision_frame': source_vision_frame,
			'target_vision_frame': target_vision_frame
		})
		write_image(target_vision_path, output_vision_frame)
		update_progress(1)


def process_image(source_path : str, target_path : str, output_path : str) -> None:
	reference_faces = get_reference_faces() if 'reference' in state_manager.get_item('face_selector_mode') else None
	source_vision_frame = read_static_image(state_manager.get_item('target_path'))
	target_vision_frame = read_static_image(target_path)
	output_vision_frame = process_frame(
	{
		'reference_faces': reference_faces,
		'source_vision_frame': source_vision_frame,
		'target_vision_frame': target_vision_frame
	})
	write_image(output_path, output_vision_frame)


def process_video(source_paths : List[str], temp_frame_paths : List[str]) -> None:
	processors.multi_process_frames(None, temp_frame_paths, process_frames)
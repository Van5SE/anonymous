# Copyright (c) OpenMMLab. All rights reserved.
__author__= 'fanxiaofeng'
import datetime
import itertools
import os.path as osp
import tempfile
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence, Union

import numpy as np
import torch
from mmengine.evaluator import BaseMetric
from mmengine.fileio import FileClient, dump, load
from mmengine.logging import MMLogger
from terminaltables import AsciiTable

from mmdet.datasets.api_wrappers import COCO, COCOeval
from mmdet.registry import METRICS
from mmdet.structures.mask import encode_mask_results
from ..functional import eval_recalls
from ..functional import compute_mAP_wild6d,plot_mAP_nocs
import _pickle as cPickle
import os

import datetime
import sys
sys.path.append("../../../")
from tools.train import parse_args as train_args
#from tools.test import parse_args as test_args
from mmengine.config import Config


@METRICS.register_module()
class CocoMetricWild6d(BaseMetric):
    """COCO evaluation metric.

    Evaluate AR, AP, and mAP for detection tasks including proposal/box
    detection and instance segmentation. Please refer to
    https://cocodataset.org/#detection-eval for more details.

    Args:
        ann_file (str, optional): Path to the coco format annotation file.
            If not specified, ground truth annotations from the dataset will
            be converted to coco format. Defaults to None.
        metric (str | List[str]): Metrics to be evaluated. Valid metrics
            include 'bbox', 'segm', 'proposal', and 'proposal_fast'.
            Defaults to 'bbox'.
        classwise (bool): Whether to evaluate the metric class-wise.
            Defaults to False.
        proposal_nums (Sequence[int]): Numbers of proposals to be evaluated.
            Defaults to (100, 300, 1000).
        iou_thrs (float | List[float], optional): IoU threshold to compute AP
            and AR. If not specified, IoUs from 0.5 to 0.95 will be used.
            Defaults to None.
        metric_items (List[str], optional): Metric result names to be
            recorded in the evaluation result. Defaults to None.
        format_only (bool): Format the output results without perform
            evaluation. It is useful when you want to format the result
            to a specific format and submit it to the test server.
            Defaults to False.
        outfile_prefix (str, optional): The prefix of json files. It includes
            the file path and the prefix of filename, e.g., "a/b/prefix".
            If not specified, a temp file will be created. Defaults to None.
        file_client_args (dict): Arguments to instantiate a FileClient.
            See :class:`mmengine.fileio.FileClient` for details.
            Defaults to ``dict(backend='disk')``.
        collect_device (str): Device name used for collecting results from
            different ranks during distributed training. Must be 'cpu' or
            'gpu'. Defaults to 'cpu'.
        prefix (str, optional): The prefix that will be added in the metric
            names to disambiguate homonymous metrics of different evaluators.
            If prefix is not provided in the argument, self.default_prefix
            will be used instead. Defaults to None.
        sort_categories (bool): Whether sort categories in annotations. Only
            used for `Objects365V1Dataset`. Defaults to False.
    """
    default_prefix: Optional[str] = 'coco'

    def __init__(self,
                 ann_file: Optional[str] = None,
                 metric: Union[str, List[str]] = 'pose',
                 classwise: bool = False,
                 proposal_nums: Sequence[int] = (100, 300, 1000),
                 iou_thrs: Optional[Union[float, Sequence[float]]] = None,
                 metric_items: Optional[Sequence[str]] = None,
                 format_only: bool = False,
                 outfile_prefix: Optional[str] = None,
                 file_client_args: dict = dict(backend='disk'),
                 collect_device: str = 'cpu',
                 prefix: Optional[str] = None,
                 sort_categories: bool = False) -> None:
        super().__init__(collect_device=collect_device, prefix=prefix)
        # coco evaluation metrics
        self.metrics = metric if isinstance(metric, list) else [metric]
        allowed_metrics = ['bbox', 'segm', 'proposal', 'proposal_fast' ,'pose']
        for metric in self.metrics:
            if metric not in allowed_metrics:
                raise KeyError(
                    "metric should be one of 'bbox', 'segm', 'proposal', 'pose"
                    f"'proposal_fast', but got {metric}.")

        # do class wise evaluation, default False
        self.classwise = classwise

        # proposal_nums used to compute recall or precision.
        self.proposal_nums = list(proposal_nums)

        # iou_thrs used to compute recall or precision.
        if iou_thrs is None:
            iou_thrs = np.linspace(
                .5, 0.95, int(np.round((0.95 - .5) / .05)) + 1, endpoint=True)
        self.iou_thrs = iou_thrs
        self.metric_items = metric_items
        self.format_only = format_only
        if self.format_only:
            assert outfile_prefix is not None, 'outfile_prefix must be not'
            'None when format_only is True, otherwise the result files will'
            'be saved to a temp directory which will be cleaned up at the end.'

        self.outfile_prefix = outfile_prefix

        self.file_client_args = file_client_args
        self.file_client = FileClient(**file_client_args)

        # if ann_file is not specified,
        # initialize coco api with the converted dataset
        if ann_file is not None:
            with self.file_client.get_local_path(ann_file) as local_path:
                self._coco_api = COCO(local_path)
                if sort_categories:
                    # 'categories' list in objects365_train.json and
                    # objects365_val.json is inconsistent, need sort
                    # list(or dict) before get cat_ids.
                    cats = self._coco_api.cats
                    sorted_cats = {i: cats[i] for i in sorted(cats)}
                    self._coco_api.cats = sorted_cats
                    categories = self._coco_api.dataset['categories']
                    sorted_categories = sorted(
                        categories, key=lambda i: i['id'])
                    self._coco_api.dataset['categories'] = sorted_categories
        else:
            self._coco_api = None

        # handle dataset lazy init
        self.cat_ids = None
        self.img_ids = None

    def fast_eval_recall(self,
                         results: List[dict],
                         proposal_nums: Sequence[int],
                         iou_thrs: Sequence[float],
                         logger: Optional[MMLogger] = None) -> np.ndarray:
        """Evaluate proposal recall with COCO's fast_eval_recall.

        Args:
            results (List[dict]): Results of the dataset.
            proposal_nums (Sequence[int]): Proposal numbers used for
                evaluation.
            iou_thrs (Sequence[float]): IoU thresholds used for evaluation.
            logger (MMLogger, optional): Logger used for logging the recall
                summary.
        Returns:
            np.ndarray: Averaged recall results.
        """
        gt_bboxes = []
        pred_bboxes = [result['bboxes'] for result in results]
        #logger.info(f'TEST results {results}')
        logger.info(f'TEST results len {len(results)} ')
        for i in range(len(self.img_ids)):
            ann_ids = self._coco_api.get_ann_ids(img_ids=self.img_ids[i])
            ann_info = self._coco_api.load_anns(ann_ids)
            if len(ann_info) == 0:
                gt_bboxes.append(np.zeros((0, 4)))
                continue
            bboxes = []
            for ann in ann_info:
                if ann.get('ignore', False) or ann['iscrowd']:
                    continue
                x1, y1, w, h = ann['bbox']
                bboxes.append([x1, y1, x1 + w, y1 + h])
            bboxes = np.array(bboxes, dtype=np.float32)
            if bboxes.shape[0] == 0:
                bboxes = np.zeros((0, 4))
            gt_bboxes.append(bboxes)
        logger.info(f'TEST pred len {len(pred_bboxes[0])} gt len {len(gt_bboxes[0])} ')
        recalls = eval_recalls(
            gt_bboxes, pred_bboxes, proposal_nums, iou_thrs, logger=logger)
        logger.info(f"TEST recall shape {recalls.shape}")
        ar = recalls.mean(axis=1)
        return ar


    def xyxy2xywh(self, bbox: np.ndarray) -> list:
        """Convert ``xyxy`` style bounding boxes to ``xywh`` style for COCO
        evaluation.

        Args:
            bbox (numpy.ndarray): The bounding boxes, shape (4, ), in
                ``xyxy`` order.

        Returns:
            list[float]: The converted bounding boxes, in ``xywh`` order.
        """

        _bbox: List = bbox.tolist()
        return [
            _bbox[0],
            _bbox[1],
            _bbox[2] - _bbox[0],
            _bbox[3] - _bbox[1],
        ]

    def results2json(self, results: Sequence[dict],
                     outfile_prefix: str) -> dict:
        """Dump the detection results to a COCO style json file.

        There are 3 types of results: proposals, bbox predictions, mask
        predictions, and they have different data types. This method will
        automatically recognize the type, and dump them to json files.

        Args:
            results (Sequence[dict]): Testing results of the
                dataset.
            outfile_prefix (str): The filename prefix of the json files. If the
                prefix is "somepath/xxx", the json files will be named
                "somepath/xxx.bbox.json", "somepath/xxx.segm.json",
                "somepath/xxx.proposal.json".

        Returns:
            dict: Possible keys are "bbox", "segm", "proposal", and
            values are corresponding filenames.
        """
        bbox_json_results = []
        segm_json_results = [] if 'masks' in results[0] else None
        for idx, result in enumerate(results):
            image_id = result.get('img_id', idx)
            labels = result['labels']
            bboxes = result['bboxes']
            scores = result['scores']
            # bbox results
            for i, label in enumerate(labels):
                data = dict()
                data['image_id'] = image_id
                data['bbox'] = self.xyxy2xywh(bboxes[i])
                data['score'] = float(scores[i])
                data['category_id'] = self.cat_ids[label]
                bbox_json_results.append(data)

            if segm_json_results is None:
                continue

            # segm results
            masks = result['masks']
            mask_scores = result.get('mask_scores', scores)
            for i, label in enumerate(labels):
                data = dict()
                data['image_id'] = image_id
                data['bbox'] = self.xyxy2xywh(bboxes[i])
                data['score'] = float(mask_scores[i])
                data['category_id'] = self.cat_ids[label]
                if isinstance(masks[i]['counts'], bytes):
                    masks[i]['counts'] = masks[i]['counts'].decode()
                data['segmentation'] = masks[i]
                segm_json_results.append(data)

        result_files = dict()
        result_files['bbox'] = f'{outfile_prefix}.bbox.json'
        result_files['proposal'] = f'{outfile_prefix}.bbox.json'
        dump(bbox_json_results, result_files['bbox'])

        if segm_json_results is not None:
            result_files['segm'] = f'{outfile_prefix}.segm.json'
            dump(segm_json_results, result_files['segm'])

        return result_files

    def gt_to_coco_json(self, gt_dicts: Sequence[dict],
                        outfile_prefix: str) -> str:
        """Convert ground truth to coco format json file.

        Args:
            gt_dicts (Sequence[dict]): Ground truth of the dataset.
            outfile_prefix (str): The filename prefix of the json files. If the
                prefix is "somepath/xxx", the json file will be named
                "somepath/xxx.gt.json".
        Returns:
            str: The filename of the json file.
        """
        print(gt_dicts)
        categories = [
            dict(id=id, name=name)
            for id, name in enumerate(self.dataset_meta['classes'])
        ]
        image_infos = []
        annotations = []

        for idx, gt_dict in enumerate(gt_dicts):
            img_id = gt_dict.get('img_id', idx)
            image_info = dict(
                id=img_id,
                width=gt_dict['width'],
                height=gt_dict['height'],
                file_name='')
            image_infos.append(image_info)
            for ann in gt_dict['anns']:
                label = ann['bbox_label']
                bbox = ann['bbox']
                coco_bbox = [
                    bbox[0],
                    bbox[1],
                    bbox[2] - bbox[0],
                    bbox[3] - bbox[1],
                ]
                rot = ann['rot']
                pos = ann['pos']
                #print("rot and pos",rot,pos)

                annotation = dict(
                    id=len(annotations) +
                    1,  # coco api requires id starts with 1
                    image_id=img_id,
                    bbox=coco_bbox,
                    iscrowd=ann.get('ignore_flag', 0),
                    category_id=int(label),
                    area=coco_bbox[2] * coco_bbox[3])
                if ann.get('mask', None):
                    mask = ann['mask']
                    # area = mask_util.area(mask)
                    if isinstance(mask, dict) and isinstance(
                            mask['counts'], bytes):
                        mask['counts'] = mask['counts'].decode()
                    annotation['segmentation'] = mask
                    # annotation['area'] = float(area)
                annotations.append(annotation)

        info = dict(
            date_created=str(datetime.datetime.now()),
            description='Coco json file converted by mmdet CocoMetric.')
        coco_json = dict(
            info=info,
            images=image_infos,
            categories=categories,
            licenses=None,
        )
        if len(annotations) > 0:
            coco_json['annotations'] = annotations
        converted_json_path = f'{outfile_prefix}.gt.json'
        dump(coco_json, converted_json_path)
        return converted_json_path

    # TODO: data_batch is no longer needed, consider adjusting the
    #  parameter position
    def process(self, data_batch: dict, data_samples: Sequence[dict]) -> None:
        """Process one batch of data samples and predictions. The processed
        results should be stored in ``self.results``, which will be used to
        compute the metrics when all batches have been processed.

        Args:
            data_batch (dict): A batch of data from the dataloader.
            data_samples (Sequence[dict]): A batch of data samples that
                contain annotations and predictions.
        """
        for data_sample in data_samples:
            result = dict()
            pred = data_sample['pred_instances']
            result['img_id'] = data_sample['img_id']
            result['bboxes'] = pred['bboxes'].cpu().numpy()
            result['scores'] = pred['scores'].cpu().numpy()
            result['labels'] = pred['labels'].cpu().numpy()
            result['rots'] = pred['rots_norm'].cpu().numpy()
            result['poses'] = pred['poses'].cpu().numpy() #输出结果
            result['sizes'] = pred['sizes'].cpu().numpy()
            # logger: MMLogger = MMLogger.get_current_instance()
            # logger.info("TEST processing now") #在预测的时候出结果
            
            # encode mask to RLE
            if 'masks' in pred:
                result['masks'] = encode_mask_results(
                    pred['masks'].detach().cpu().numpy()) if isinstance(
                        pred['masks'], torch.Tensor) else pred['masks']
            # some detectors use different scores for bbox and mask
            if 'mask_scores' in pred:
                result['mask_scores'] = pred['mask_scores'].cpu().numpy()

            # parse gt
            gt = dict()
            gt['width'] = data_sample['ori_shape'][1]
            gt['height'] = data_sample['ori_shape'][0]
            gt['img_id'] = data_sample['img_id']
            # print(result['rots'],"result rot<>gt",gt)
            #print(result['pos'],"result pos<>gt",gt,len(result['pos']),len(result),len(result['rots']))
            if self._coco_api is None:
                # TODO: Need to refactor to support LoadAnnotations
                assert 'instances' in data_sample, \
                    'ground truth is required for evaluation when ' \
                    '`ann_file` is not provided'
                gt['anns'] = data_sample['instances']
            # add converted result to the results list
            self.results.append((gt, result))

    def compute_metrics(self, results: list) -> Dict[str, float]:
        """Compute the metrics from processed results.

        Args:
            results (list): The processed results of each batch.

        Returns:
            Dict[str, float]: The computed metrics. The keys are the names of
            the metrics, and the values are corresponding results.
        """
        logger: MMLogger = MMLogger.get_current_instance()

        # split gt and prediction list
        gts, preds = zip(*results)
        logger.info("TEST computing metrics on Wild6d now")
        #logger.info(f"TEST gts {gts}") #输出gts width height img_id 等信息没有gt pose
        #logger.info("TEST preds",preds)#输出预测结果  包括img_id bboxes scores labels rot pos

        tmp_dir = None
        if self.outfile_prefix is None:
            tmp_dir = tempfile.TemporaryDirectory()
            outfile_prefix = osp.join(tmp_dir.name, 'results')
        else:
            outfile_prefix = self.outfile_prefix

        #logger.info("TEST coco_api",self._coco_api) #不为空

        if self._coco_api is None: #如果gt没有初始化就要先把gt存进来 不然就在评估的时候根据img_id读取gt
            # use converted gt json file to initialize coco api
            logger.info('Converting ground truth to coco format...')
            coco_json_path = self.gt_to_coco_json(
                gt_dicts=gts, outfile_prefix=outfile_prefix)
            self._coco_api = COCO(coco_json_path)

        # handle lazy init
        if self.cat_ids is None:
            self.cat_ids = self._coco_api.get_cat_ids(
                cat_names=self.dataset_meta['classes'])
        if self.img_ids is None:
            self.img_ids = self._coco_api.get_img_ids()

        # convert predictions to coco format and dump to json file
        result_files = self.results2json(preds, outfile_prefix)
        #logger.info(f"TEST result_files {result_files}")
        #logger.info(f"TEST outfile_prefix {self.outfile_prefix}")

        eval_results = OrderedDict()
        if self.format_only:
            logger.info('results are saved in '
                        f'{osp.dirname(outfile_prefix)}')
            return eval_results

        for metric in self.metrics:
            logger.info(f'Evaluating {metric}...')

            # TODO: May refactor fast_eval_recall to an independent metric?
            # fast eval recall
            if metric == 'proposal_fast':
                ar = self.fast_eval_recall(
                    preds, self.proposal_nums, self.iou_thrs, logger=logger)
                log_msg = []
                for i, num in enumerate(self.proposal_nums):
                    eval_results[f'AR@{num}'] = ar[i]
                    log_msg.append(f'\nAR@{num}\t{ar[i]:.4f}')
                log_msg = ''.join(log_msg)
                logger.info(log_msg)
                continue

            if metric == 'pose':

                # follow object-deformnet build pred_results
                pred_results = [] #把所有图片的信息存成一个list
                try: 
                    args=train_args() # on training
                    #args=test_args()
                    # load config
                    cfg = Config.fromfile(args.config)
                    if args.work_dir is not None:
                        # update configs according to CLI args if args.work_dir is not None
                        cfg.work_dir = args.work_dir
                    elif cfg.get('work_dir', None) is None:
                        # use config filename as default work_dir if cfg.work_dir is None
                        cfg.work_dir = osp.join('./work_dirs',
                                                osp.splitext(osp.basename(args.config))[0])
                    #print(cfg.work_dir)
                    pred_results_base_dir = osp.join(cfg.work_dir,'pred_results/wild6d/')
                except:
                    pred_results_base_dir='pred_results/wild6d/eval'
                #print("pred_results_base_dir",pred_results_base_dir)
                #pred_results_base_dir='pred_results/wild6d/spilts_xyz/'
                pred_results_dir=osp.join(pred_results_base_dir,str(datetime.datetime.now()))
                print("pred_results_dir",pred_results_dir)
                if not osp.exists(pred_results_dir):
                    os.makedirs(pred_results_dir)

                #print(len(self.img_ids),len(preds))
                #print(type(preds))
                #preds=preds['scores']>0.3
                for i,result in zip(range(len(self.img_ids)),preds):
                    pred_result={} #存储一张图的gt和pred
                    ann_ids = self._coco_api.get_ann_ids(img_ids=self.img_ids[i])
                    ann_info = self._coco_api.load_anns(ann_ids)
                    #logger.info(f"TEST ann_ids {ann_ids} ann_info {ann_info}")
                    if len(ann_info) == 0:
                        pred_results.append(pred_result)
                        continue
                    gt_rot = [] #一张图片里的GT
                    gt_pos = []
                    gt_label = []
                    gt_bbox = []
                    gt_size = []
                    gt_handle_visibility = []
                    for ann in ann_info:
                        if ann.get('ignore', False):
                            continue
                        gt_rot.append(ann['relative_pose'].get('rotation'))
                        gt_pos.append(ann['relative_pose'].get('position'))
                        x1, y1, w, h = ann['bbox']
                        gt_bbox.append([x1, y1, x1 + w, y1 + h])
                        gt_size.append(ann['bbox_3d_size'])
                        gt_label.append(ann['category_id'])
                        gt_handle_visibility.append(ann['handle_visibility'])
                    #print(result)
                    
                    score_ids=result['scores']>0.3 #只取置信度大于0.3的结果
                    pred_rot = result['rots'][score_ids]
                    pred_pos = result['poses'][score_ids]
                    pred_size = result['sizes'][score_ids]
                    pred_score = result['scores'][score_ids]
                    pred_label = result['labels'][score_ids]
                    pred_bbox = result['bboxes'][score_ids]

                    # pred_rot = result['rots'] #把所有预测结果用来评估不会提升ap 会使acc降低很多
                    # pred_pos = result['poses']
                    # pred_size = result['sizes']
                    # pred_score = result['scores']
                    # pred_label = result['labels']
                    # pred_bbox = result['bboxes']

                    #concat R and T
                    homo_axis=[0,0,0,1]
                    gt_rot=np.array(gt_rot,dtype=np.float32).reshape(-1,3,3)
                    gt_pos=np.array(gt_pos,dtype=np.float32).reshape(-1,3,1)
                    homo_array=np.array(homo_axis*gt_rot.shape[0],dtype=np.float32).reshape(-1,1,4)
                    gt_RT=np.concatenate([gt_rot,gt_pos],axis=2)
                    gt_RT=np.concatenate([gt_RT,homo_array],axis=1)

                    pred_rot=np.array(pred_rot,dtype=np.float32).reshape(-1,3,3)
                    pred_pos=np.array(pred_pos,dtype=np.float32).reshape(-1,3,1)
                    homo_array=np.array(homo_axis*pred_rot.shape[0],dtype=np.float32).reshape(-1,1,4) #gt和pred之间的长度不等
                    pred_RT=np.concatenate([pred_rot,pred_pos],axis=2)
                    pred_RT=np.concatenate([pred_RT,homo_array],axis=1)
                    #np.array other list
                    gt_label=np.array(gt_label,np.int32)
                    gt_bbox=np.array(gt_bbox,dtype=np.int32)
                    gt_size=np.array(gt_size,dtype=np.float32)
                    gt_handle_visibility=np.array(gt_handle_visibility)
                    pred_label=np.array(pred_label,dtype=np.int32)
                    pred_bbox=np.array(pred_bbox,dtype=np.int32)
                    pred_size=np.array(pred_size,dtype=np.float32)
                    pred_score=np.array(pred_score,np.float32)

                    #generate pred_result
                    pred_result['gt_class_ids']=gt_label
                    pred_result['gt_bboxes']=gt_bbox
                    pred_result['gt_RTs']=gt_RT
                    pred_result['gt_scales']=gt_size
                    pred_result['gt_handle_visibility']=gt_handle_visibility
                    pred_result['pred_class_ids']=pred_label
                    pred_result['pred_bboxes']=pred_bbox
                    pred_result['pred_scores']=pred_score
                    pred_result['pred_RTs']=pred_RT
                    pred_result['pred_scales']=pred_size

                    #print(pred_result)
                    #add each img result to list
                    pred_results.append(pred_result)

                #store the result
                # with open(pred_results_dir+'/pred_results.txt','w') as f:
                #     f.write(str(pred_results))
                # with open(pred_results_dir+'/pred_results.pkl','wb') as f:
                #     cPickle.dump(pred_results, f)
                
                #comupte mAP
                evaluate_wild6d(pred_results,pred_results_dir,logger)
                
                continue

            # evaluate proposal, bbox and segm
            iou_type = 'bbox' if metric == 'proposal' else metric
            if metric not in result_files:
                raise KeyError(f'{metric} is not in results')
            try:
                predictions = load(result_files[metric])
                if iou_type == 'segm':
                    # Refer to https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/coco.py#L331  # noqa
                    # When evaluating mask AP, if the results contain bbox,
                    # cocoapi will use the box area instead of the mask area
                    # for calculating the instance area. Though the overall AP
                    # is not affected, this leads to different
                    # small/medium/large mask AP results.
                    for x in predictions:
                        x.pop('bbox')
                coco_dt = self._coco_api.loadRes(predictions)

            except IndexError:
                logger.error(
                    'The testing results of the whole dataset is empty.')
                break

            coco_eval = COCOeval(self._coco_api, coco_dt, iou_type)

            coco_eval.params.catIds = self.cat_ids
            coco_eval.params.imgIds = self.img_ids
            coco_eval.params.maxDets = list(self.proposal_nums)
            coco_eval.params.iouThrs = self.iou_thrs

            # mapping of cocoEval.stats
            coco_metric_names = {
                'mAP': 0,
                'mAP_50': 1,
                'mAP_75': 2,
                'mAP_s': 3,
                'mAP_m': 4,
                'mAP_l': 5,
                'AR@100': 6,
                'AR@300': 7,
                'AR@1000': 8,
                'AR_s@1000': 9,
                'AR_m@1000': 10,
                'AR_l@1000': 11
            }
            metric_items = self.metric_items
            if metric_items is not None:
                for metric_item in metric_items:
                    if metric_item not in coco_metric_names:
                        raise KeyError(
                            f'metric item "{metric_item}" is not supported')

            if metric == 'proposal':
                coco_eval.params.useCats = 0
                coco_eval.evaluate()
                coco_eval.accumulate()
                coco_eval.summarize()
                if metric_items is None:
                    metric_items = [
                        'AR@100', 'AR@300', 'AR@1000', 'AR_s@1000',
                        'AR_m@1000', 'AR_l@1000'
                    ]

                for item in metric_items:
                    val = float(
                        f'{coco_eval.stats[coco_metric_names[item]]:.3f}')
                    eval_results[item] = val
            else:
                coco_eval.evaluate()
                coco_eval.accumulate()
                coco_eval.summarize()
                if self.classwise:  # Compute per-category AP
                    # Compute per-category AP
                    # from https://github.com/facebookresearch/detectron2/
                    precisions = coco_eval.eval['precision']
                    # precision: (iou, recall, cls, area range, max dets)
                    assert len(self.cat_ids) == precisions.shape[2]

                    results_per_category = []
                    for idx, cat_id in enumerate(self.cat_ids):
                        # area range index 0: all area ranges
                        # max dets index -1: typically 100 per image
                        nm = self._coco_api.loadCats(cat_id)[0]
                        precision = precisions[:, :, idx, 0, -1]
                        precision = precision[precision > -1]
                        if precision.size:
                            ap = np.mean(precision)
                        else:
                            ap = float('nan')
                        results_per_category.append(
                            (f'{nm["name"]}', f'{round(ap, 3)}'))
                        eval_results[f'{nm["name"]}_precision'] = round(ap, 3)

                    num_columns = min(6, len(results_per_category) * 2)
                    results_flatten = list(
                        itertools.chain(*results_per_category))
                    headers = ['category', 'AP'] * (num_columns // 2)
                    results_2d = itertools.zip_longest(*[
                        results_flatten[i::num_columns]
                        for i in range(num_columns)
                    ])
                    table_data = [headers]
                    table_data += [result for result in results_2d]
                    table = AsciiTable(table_data)
                    logger.info('\n' + table.table)

                if metric_items is None:
                    metric_items = [
                        'mAP', 'mAP_50', 'mAP_75', 'mAP_s', 'mAP_m', 'mAP_l'
                    ]

                for metric_item in metric_items:
                    key = f'{metric}_{metric_item}'
                    val = coco_eval.stats[coco_metric_names[metric_item]]
                    eval_results[key] = float(f'{round(val, 3)}')

                ap = coco_eval.stats[:6]
                logger.info(f'{metric}_mAP_copypaste: {ap[0]:.3f} '
                            f'{ap[1]:.3f} {ap[2]:.3f} {ap[3]:.3f} '
                            f'{ap[4]:.3f} {ap[5]:.3f}')

        if tmp_dir is not None:
            tmp_dir.cleanup()
        return eval_results


def evaluate_wild6d(pred_results=None,pred_results_dir=None,logger=None):
    degree_thres_list = list(range(0, 61, 1))
    shift_thres_list = [i / 2 for i in range(21)]
    iou_thres_list = [i / 100 for i in range(101)]
    #load predictions
    if pred_results==None:
        result_pkl_path='/root/userfolder/github/mmdetection/pred_results/wild6d/pred_results.pkl'
        with open(result_pkl_path, 'rb') as f:
            pred_results = cPickle.load(f)
    else:
        pred_results=pred_results

    if pred_results_dir==None:
        result_dir='/root/userfolder/github/mmdetection/pred_results/wild6d/'
    else:
        result_dir=pred_results_dir

    # To be consistent with wild6d, set use_matches_for_pose=True for mAP evaluation
    iou_aps, pose_aps, iou_acc, pose_acc = compute_mAP_wild6d(pred_results, result_dir, degree_thres_list, shift_thres_list,
                                                       iou_thres_list, iou_pose_thres=0.1, use_matches_for_pose=True, 
                                                       select_class='laptop')
    # metric
    fw = open('{0}/eval_logs.txt'.format(result_dir), 'a')
    iou_25_idx = iou_thres_list.index(0.25)
    iou_50_idx = iou_thres_list.index(0.5)
    iou_75_idx = iou_thres_list.index(0.75)
    degree_05_idx = degree_thres_list.index(5)
    degree_10_idx = degree_thres_list.index(10)
    shift_02_idx = shift_thres_list.index(2)
    shift_05_idx = shift_thres_list.index(5)
    shift_10_idx = shift_thres_list.index(10)
    messages = []
    messages.append('mAP:')
    messages.append('3D IoU at 25: {:.1f}'.format(iou_aps[-1, iou_25_idx] * 100))
    messages.append('3D IoU at 50: {:.1f}'.format(iou_aps[-1, iou_50_idx] * 100))
    messages.append('3D IoU at 75: {:.1f}'.format(iou_aps[-1, iou_75_idx] * 100))
    messages.append('5 degree, 2cm: {:.1f}'.format(pose_aps[-1, degree_05_idx, shift_02_idx] * 100))
    messages.append('5 degree, 5cm: {:.1f}'.format(pose_aps[-1, degree_05_idx, shift_05_idx] * 100))
    messages.append('10 degree, 2cm: {:.1f}'.format(pose_aps[-1, degree_10_idx, shift_02_idx] * 100))
    messages.append('10 degree, 5cm: {:.1f}'.format(pose_aps[-1, degree_10_idx, shift_05_idx] * 100))
    messages.append('10 degree, 10cm: {:.1f}'.format(pose_aps[-1, degree_10_idx, shift_10_idx] * 100))
    messages.append('10 degree: {:.1f}'.format(pose_aps[-1, degree_10_idx, -1] * 100))
    messages.append('10cm: {:.1f}'.format(pose_aps[-1, -1, shift_10_idx] * 100))

    for i in range(pose_aps.shape[0]):
        messages.append('mAP:')
        messages.append('{:d} 3D IoU at 25: {:.1f}'.format(i,iou_aps[i, iou_25_idx] * 100))
        messages.append('{:d} 3D IoU at 50: {:.1f}'.format(i,iou_aps[i, iou_50_idx] * 100))
        messages.append('{:d} 3D IoU at 75: {:.1f}'.format(i,iou_aps[i, iou_75_idx] * 100))
        messages.append('{:d} 5 degree, 2cm: {:.1f}'.format(i,pose_aps[i, degree_05_idx, shift_02_idx] * 100))
        messages.append('{:d} 5 degree, 5cm: {:.1f}'.format(i,pose_aps[i, degree_05_idx, shift_05_idx] * 100))
        messages.append('{:d} 10 degree, 2cm: {:.1f}'.format(i,pose_aps[i, degree_10_idx, shift_02_idx] * 100))
        messages.append('{:d} 10 degree, 5cm: {:.1f}'.format(i,pose_aps[i, degree_10_idx, shift_05_idx] * 100))
        messages.append('10 degree, 10cm: {:.1f}'.format(pose_aps[i, degree_10_idx, shift_10_idx] * 100))
        messages.append('10 degree: {:.1f}'.format(pose_aps[i, degree_10_idx, -1] * 100))
        messages.append('10cm: {:.1f}'.format(pose_aps[i, -1, shift_10_idx] * 100))

    messages.append('Acc:')
    messages.append('3D IoU at 25: {:.1f}'.format(iou_acc[-1, iou_25_idx] * 100))
    messages.append('3D IoU at 50: {:.1f}'.format(iou_acc[-1, iou_50_idx] * 100))
    messages.append('3D IoU at 75: {:.1f}'.format(iou_acc[-1, iou_75_idx] * 100))
    messages.append('5 degree, 2cm: {:.1f}'.format(pose_acc[-1, degree_05_idx, shift_02_idx] * 100))
    messages.append('5 degree, 5cm: {:.1f}'.format(pose_acc[-1, degree_05_idx, shift_05_idx] * 100))
    messages.append('10 degree, 2cm: {:.1f}'.format(pose_acc[-1, degree_10_idx, shift_02_idx] * 100))
    messages.append('10 degree, 5cm: {:.1f}'.format(pose_acc[-1, degree_10_idx, shift_05_idx] * 100))
    messages.append('10 degree, 10cm: {:.1f}'.format(pose_acc[-1, degree_10_idx, shift_10_idx] * 100))
    messages.append('10 degree: {:.1f}'.format(pose_acc[-1, degree_10_idx, -1] * 100))
    messages.append('10cm: {:.1f}'.format(pose_acc[-1, -1, shift_10_idx] * 100))
    for msg in messages:
        #print(msg)
        logger.info(msg) #使用logger打印信息
        fw.write(msg + '\n')
    fw.close()
    # load wild6d results
    pkl_path = os.path.join(result_dir, 'mAP_Acc.pkl')
    with open(pkl_path, 'rb') as f:
        wild6d_results = cPickle.load(f)
    wild6d_iou_aps = wild6d_results['iou_aps'][-1, :]
    wild6d_pose_aps = wild6d_results['pose_aps'][-1, :, :]
    iou_aps = np.concatenate((iou_aps, wild6d_iou_aps[None, :]), axis=0)
    pose_aps = np.concatenate((pose_aps, wild6d_pose_aps[None, :, :]), axis=0)
    # plot
    plot_mAP_nocs(iou_aps, pose_aps, result_dir, iou_thres_list, degree_thres_list, shift_thres_list)
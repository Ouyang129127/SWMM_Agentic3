"""Deterministic map labels, using actual rendered text boxes and node positions.

Call after figure/axes layout is final. Coordinates are in the axes' data space.
No node-specific offsets or model IDs are required. The returned layout can be
stored with a report's internal QA records.
"""
import math
import numpy as np
from matplotlib.transforms import Bbox


def _overlap(a, b):
    return a.x0 < b.x1 and a.x1 > b.x0 and a.y0 < b.y1 and a.y1 > b.y0


def place_node_labels(ax, points, *, fontsize=8, margin_px=5, padding_px=3):
    """Place all labels without collisions or crossing the axes boundary.

    points: [{"label": str, "x": float, "y": float}, ...]. Closely spaced
    nodes go first; each label tries increasingly distant anchor positions.
    Raises rather than silently dropping a node if the plot has no free space.
    """
    if not points:
        return []
    labels=[p['label'] for p in points]
    if len(labels)!=len(set(labels)):
        raise ValueError('Node labels must be unique')
    if any(not math.isfinite(p[k]) for p in points for k in ('x','y')):
        raise ValueError('Node coordinates must be finite')
    fig=ax.figure
    fig.canvas.draw()
    renderer=fig.canvas.get_renderer()
    data=np.asarray([[p['x'],p['y']] for p in points],dtype=float)
    pixel=ax.transData.transform(data)
    bounds=ax.get_window_extent(renderer)
    distances=np.linalg.norm(pixel[:,None,:]-pixel[None,:,:],axis=2)
    crowding=((distances<45)&(distances>0)).sum(axis=1)
    order=sorted(range(len(points)),key=lambda i:(-int(crowding[i]),points[i]['label']))
    placed=[]
    result=[]
    node_boxes=[Bbox.from_extents(x-4,y-4,x+4,y+4) for x,y in pixel]
    angles=[45,135,-45,-135,0,180,90,-90,22.5,157.5,-22.5,-157.5,67.5,112.5,-67.5,-112.5]
    for index in order:
        point=points[index]
        artist=ax.annotate(point['label'],(point['x'],point['y']),xytext=(0,0),
            textcoords='offset points',ha='center',va='center',fontsize=fontsize,
            color='#153d36',zorder=7,annotation_clip=False,
            bbox={'facecolor':'white','alpha':.94,'edgecolor':'none','pad':1.8},
            arrowprops={'arrowstyle':'-','lw':.65,'color':'#54726b','shrinkA':2,'shrinkB':3})
        found=False
        radii=range(14,int(max(bounds.width,bounds.height)*72/fig.dpi),6)
        for radius in radii:
            for angle in angles:
                dx=radius*math.cos(math.radians(angle))
                dy=radius*math.sin(math.radians(angle))
                artist.set_position((dx,dy))
                artist.update_positions(renderer)
                artist.update_bbox_position_size(renderer)
                box=artist.get_bbox_patch().get_window_extent(renderer)
                padded=Bbox.from_extents(box.x0-padding_px,box.y0-padding_px,
                                         box.x1+padding_px,box.y1+padding_px)
                inside=(padded.x0>=bounds.x0+margin_px and padded.x1<=bounds.x1-margin_px
                    and padded.y0>=bounds.y0+margin_px and padded.y1<=bounds.y1-margin_px)
                if not inside or any(_overlap(padded,b) for b in placed+node_boxes):
                    continue
                placed.append(padded)
                result.append({'label':point['label'],'x':float(point['x']),'y':float(point['y']),
                    'offset_points':[round(dx,6),round(dy,6)],
                    'text_box_px':[round(float(v),4) for v in box.extents],
                    'axes_box_px':[round(float(v),4) for v in bounds.extents]})
                found=True
                break
            if found:
                break
        if not found:
            artist.remove()
            raise ValueError(f'No free map space for label {point["label"]}; use a larger figure or a local inset')
    fig.canvas.draw()
    return sorted(result,key=lambda r:r['label'])

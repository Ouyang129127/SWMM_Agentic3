"""Deterministic charts for arbitrary event sets and simulation time ranges."""
import threading
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from matplotlib.colors import LinearSegmentedColormap, Normalize, ListedColormap
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

from workflow_agents.reporting.labels import place_node_labels
from workflow_agents.reporting.scene import _clock, _timestamp
from workflow_agents.reporting.surface import BASIS as SURFACE_BASIS


LOCK=threading.Lock()  # pyplot is shared by simultaneous web requests.
TEAL='#087b72'; ORANGE='#d78034'; BLUE='#607a99'


def _rainfall_figure(materials, rain, cumulative):
    """Use the agreed example's typography and direct peak annotation."""
    with plt.rc_context({'font.family':['Microsoft YaHei','DejaVu Sans'],
            'font.size':11,'axes.unicode_minus':False,'axes.spines.top':False,
            'axes.spines.right':False,'axes.edgecolor':'#ced9d7','axes.labelcolor':'#485f59',
            'xtick.color':'#60756f','ytick.color':'#60756f','figure.facecolor':'white',
            'axes.facecolor':'white','savefig.facecolor':'white'}):
        elapsed=(rain.timestamp-rain.timestamp.iloc[0]).dt.total_seconds().to_numpy()/60
        widths=np.diff(elapsed)
        values=rain.value.to_numpy()[:-1]
        peak=int(np.argmax(values))
        intensity=float(values[peak])
        interval=float(widths[peak])
        duration=max(float(elapsed[-1]),1.)
        fig,ax=plt.subplots(figsize=(10.2,3.15))
        ax.bar(elapsed[:-1],values,width=widths*.84,align='edge',color=TEAL,alpha=.84)
        upper=MaxNLocator(nbins=7).tick_values(0,max(intensity*1.15,1.))[-1]
        ax.set(xlabel='降雨开始后的时间（分钟）',ylabel=f'{interval:g}分钟平均雨强（毫米/小时）',
               xlim=(0,duration),ylim=(0,upper))
        ax.xaxis.set_major_locator(MaxNLocator(nbins=8))
        ax.grid(axis='y',color='#e7eeeb',lw=.8);ax.set_axisbelow(True)
        if intensity>0:
            origin=_timestamp(str(rain.timestamp.iloc[0]))
            timestamp=_timestamp(str(rain.timestamp.iloc[peak])).replace(second=0,microsecond=0)
            peak_time=_clock(timestamp,origin)
            right=elapsed[peak]<=duration*.65
            label_x=min(elapsed[peak]+duration*.145,duration*.72) if right else max(elapsed[peak]-duration*.145,duration*.28)
            ax.annotate(f'雨峰 {peak_time}\n{intensity:.2f} 毫米/小时',
                xy=(elapsed[peak]+widths[peak]*.4,intensity),
                xytext=(label_x,upper*.9),ha='left' if right else 'right',
                fontsize=10,color=TEAL,arrowprops={'arrowstyle':'-','color':TEAL})
        ax2=ax.twinx();ax2.plot(elapsed,cumulative,color=ORANGE,lw=2)
        ax2.set(ylabel='累计雨量（毫米）',ylim=(0,max(float(cumulative[-1])*1.1875,1.)))
        ax2.yaxis.label.set_color(ORANGE);ax2.tick_params(axis='y',colors=ORANGE)
        caption=f'降雨过程：绿色柱表示{interval:g}分钟平均雨强，橙色曲线表示累计雨量。'
        return fig,caption


def make_rainfall_plot(materials, rain, cumulative, output):
    """Generate just this section's image without rendering other report plots."""
    with LOCK:
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        fig,caption=_rainfall_figure(materials,rain,cumulative)
        path=output/'rainfall.png'
        fig.savefig(path,dpi=160,bbox_inches='tight');plt.close(fig)
        return {'path':str(path),'caption':caption}


def make_plots(materials, data, output):
    with LOCK,plt.rc_context({'font.family':['Microsoft YaHei','DejaVu Sans'],
            'axes.unicode_minus':False,'font.size':10,'axes.spines.top':False,
            'axes.spines.right':False,'axes.edgecolor':'#ced9d7','axes.labelcolor':'#485f59',
            'figure.facecolor':'white','savefig.facecolor':'white'}):
        return _draw(materials,data,Path(output))


def _draw(materials,d,output):
    output.mkdir(parents=True,exist_ok=True); media={}; layouts=[]
    def save(fig,key,caption,dpi=150):
        path=output/f'{key}.png'
        fig.savefig(path,dpi=dpi,bbox_inches='tight');plt.close(fig)
        media[key]={'path':str(path),'caption':caption}
    def style(ax):
        ax.grid(axis='y',color='#e7eeeb',lw=.7);ax.set_axisbelow(True)
    fig,caption=_rainfall_figure(materials,d['rain_frame'],d['cumulative_rain'])
    save(fig,'rainfall',caption,dpi=160)
    times=d['times'];mins=(times-times[0]).total_seconds()/60
    fig,axs=plt.subplots(3,1,figsize=(10.2,5.7),sharex=True,gridspec_kw={'hspace':.16})
    axs[0].plot(mins,d['areas'][.01],color=TEAL,lw=2);axs[0].fill_between(mins,d['areas'][.01],color=TEAL,alpha=.12)
    axs[0].set_ylabel('积水面积\n（平方米）')
    axs[1].plot(mins,d['areas'][.15],color=BLUE,lw=2);axs[1].fill_between(mins,d['areas'][.15],color=BLUE,alpha=.15)
    axs[1].set_ylabel('内涝面积\n（平方米）')
    axs[2].plot(mins,d['max_depth']*100,color=ORANGE,lw=2);axs[2].set_ylabel('最大水深\n（厘米）')
    rain_end=(pd.Timestamp(materials['scene']['rain_end'])-times[0]).total_seconds()/60
    ticks=np.unique(np.linspace(0,float(mins[-1]),min(8,len(times))).round())
    axs[-1].set_xticks(ticks,[(times[0]+pd.Timedelta(minutes=float(t))).strftime('%m-%d %H:%M' if times[0].date()!=times[-1].date() else '%H:%M') for t in ticks])
    axs[-1].set_xlabel('模拟时间')
    for ax in axs:
        style(ax);ax.axvline(rain_end,color='#9daaa4',ls=':',lw=1)
        ax.set_ylim(bottom=0)
    save(fig,'surface_process','地表积水面积、内涝面积和最大水深的变化。虚线表示降雨结束。')

    base=np.full(d['grid'].shape,np.nan)
    base[d['flow']]=0;base[d['road']]=1;base[d['building']&d['valid']]=2
    land=ListedColormap(['#e5eee1','#c6d0d0','#556468'])
    water=LinearSegmentedColormap.from_list('water_depth',SURFACE_BASIS['palette'])
    upper=max(.22,float(np.ceil(d['max_depth'].max()*100)/100))
    norm=Normalize(vmin=.01,vmax=upper)
    points=[]
    for node in dict.fromkeys(e['node_id'] for e in materials['events']):
        pos=d['mapping'][d['mapping'].node_id==node].iloc[0]
        points.append({'label':node,'x':float(pos.cell_col),'y':float(pos.cell_row)})
    def map_ax(ax,array,title,nodes=False):
        ax.imshow(np.ma.masked_invalid(base),cmap=land,vmin=0,vmax=2,interpolation='nearest')
        im=ax.imshow(np.ma.masked_where((array<.01)|(~d['flow']),array),cmap=water,norm=norm,interpolation='nearest')
        if nodes:
            for p in points:ax.scatter(p['x'],p['y'],s=12,c=TEAL,edgecolors='white',lw=.5,zorder=5)
        ax.set_title(title,fontsize=11);ax.set_xticks([]);ax.set_yticks([])
        for spine in ax.spines.values():spine.set_visible(False)
        return im
    legend=[]
    if d['classification_available']: legend.append(Patch(color='#c6d0d0',label='道路'))
    legend += [Patch(color='#e5eee1',label='绿地' if d['classification_available'] else '地表'),Patch(color='#556468',label='建筑物')]
    for scale in (1,1.4,2):
        fig,axs=plt.subplots(1,2,figsize=(10.2*scale,6.3*scale))
        map_ax(axs[0],d['depth'].max(axis=0),'模拟过程中各位置的最大水深',True)
        im=map_ax(axs[1],d['depth'][-1],f"模拟结束时的水深 · {times[-1].strftime('%H:%M')}")
        fig.legend(handles=legend,loc='lower center',ncol=3,frameon=False,fontsize=10)
        fig.subplots_adjust(bottom=.10,right=.89,wspace=.12)
        cax=fig.add_axes([.92,.19,.016,.59])
        fig.colorbar(im,cax=cax).set_label('水深（米）',fontsize=10)
        try:layouts=place_node_labels(axs[0],points);break
        except ValueError:
            plt.close(fig)
            if scale==2:raise
    save(fig,'surface_maps','模拟过程中各位置的最大水深与模拟结束时的水深。颜色由浅黄至深红表示水深增加。')
    phases=d['phase_indices'];cols=2 if len(phases)==4 else min(3,len(phases));rows=int(np.ceil(len(phases)/cols))
    fig,axes=plt.subplots(rows,cols,figsize=(4.1*cols,4.4*rows),squeeze=False)
    for ax,i in zip(axes.flat,phases):
        clock=_clock(_timestamp(str(times[i])),_timestamp(str(times[0])))
        im=map_ax(ax,d['depth'][i],f"{clock}\n积水 {d['areas'][.01][i]:,.0f}平方米 · 内涝 {d['areas'][.15][i]:,.0f}平方米")
    for ax in list(axes.flat)[len(phases):]:ax.set_visible(False)
    fig.legend(handles=legend,loc='lower center',ncol=3,frameon=False,fontsize=9)
    fig.subplots_adjust(bottom=.08,right=.89,hspace=.12)
    cax=fig.add_axes([.92,.20,.015,.60])
    fig.colorbar(im,cax=cax).set_label('水深（米）',fontsize=10)
    save(fig,'surface_stages','地表积水从出现、扩展到雨停及模拟结束的阶段变化。')
    if materials['events']:
        grouped={}
        for e in materials['events']:grouped[e['node_id']]=grouped.get(e['node_id'],0)+e['event']['estimated_volume_m3']
        ordered=sorted(grouped,key=lambda n:grouped[n])
        fig,ax=plt.subplots(figsize=(10.2,max(2.4,.36*len(ordered)+1)))
        ax.barh(ordered,[grouped[n] for n in ordered],color=TEAL)
        ax.set_xlabel('累计冒溢量（立方米）');style(ax)
        save(fig,'overflow_volumes','本次诊断范围内各节点的累计冒溢量；同一节点多次事件合计。')
    for index,event in enumerate(materials['events']):
        process=event['process'];st=pd.to_datetime([s['time'] for s in process]);x=(st-st[0]).total_seconds()/60
        fig,(ax,axd)=plt.subplots(1,2,figsize=(10.2,2.8),gridspec_kw={'width_ratios':[1.7,1],'wspace':.3})
        for field,color,label in [('total_inflow_Ls',TEAL,'总入流'),('outflow_Ls',BLUE,'直接相连管段排出量'),('flooding_Ls',ORANGE,'冒溢流量')]:
            ax.plot(x,[s[field] for s in process],lw=1.8,marker='o',ms=3,color=color,label=label)
        ax.set_ylabel('流量（升/秒）');ax.set_ylim(bottom=0);style(ax)
        ax.legend(loc='upper center',bbox_to_anchor=(.5,1.25),ncol=3,frameon=False,fontsize=8)
        axd.plot(x,[s['depth_m'] for s in process],color=TEAL,lw=1.8,marker='o',ms=3)
        axd.set_ylabel('节点水深（米）');axd.set_ylim(bottom=0);style(axd)
        for a in (ax,axd):
            a.set_xticks(x,[t.strftime('%H:%M') for t in st],rotation=30,fontsize=8)
            a.set_xlabel('模拟时间')
            a.axvspan((pd.Timestamp(event['event']['start'])-st[0]).total_seconds()/60,
                         (pd.Timestamp(event['event']['end'])-st[0]).total_seconds()/60,color=TEAL,alpha=.09)
        save(fig,f'event_{index}',f"{event['node_id']}的入流、排出量、冒溢及节点水深过程。浅色背景为本次冒溢事件时段。")
    if d['gif']:
        media['animation']={'path':str(d['gif']),'caption':'保存的地表演变动画，用于观察积水出现、扩展和后期变化；动画采用原始连续水深色标。'}
    return media,layouts

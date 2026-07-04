namespace Zig.Property;

table 11024121 RealtyObjectFacilityFDN
{
    Caption = 'Lettable Object Facilities', Comment = 'DevOps221157';
    DataClassification = CustomerContent;
    LookupPageId = FacilitiesOfRealtyObjectFDN;
    Permissions =
        tabledata OGE = R,
        tabledata RealtyObjectFacilityFDN = RM;

    fields
    {
        field(1; RealtyObjectNo; Code[20])
        {
            Caption = 'Lettable Object No.', Comment = 'DevOps221157';
            TableRelation = OGE;
            ToolTip = 'Specifies the number of the lettable object the facility belongs to.', Comment = 'DevOps221157';

            trigger OnValidate()
            begin
                Rec.CalcFields(RealtyObjectAddress);
            end;
        }
        field(2; EntryNo; Integer)
        {
            Caption = 'Entry No.', Comment = 'DevOps221157';
            Editable = false;
            ToolTip = 'Shows the unique entry number of the facility of the lettable object.', Comment = 'DevOps221157';
        }
        field(3; FacilityCode; Code[10])
        {
            Caption = 'Facility Code', Comment = 'DevOps221157';
            NotBlank = true;
            TableRelation = FacilityFDN;
            ToolTip = 'Specifies the code of the facility.', Comment = 'DevOps221157';

            trigger OnValidate()
            begin
                if xRec.FacilityCode <> '' then
                    EmptyOldRealtyObjectFacilityValues();
                Rec.CalcFields(FacilityGroup, FacilityType, FacilityName, FacilityDescription, FacilityMeasuringUnit, FacilityMeasuringValueType);
                if FacilityMeasuringValueType = FacilityMeasuringValueType::Integer then
                    FacilityMeasuringValueInteger := 1;
            end;
        }
        field(4; FacilityGroup; Enum RealtyObjectFacilityGroupFDN)
        {
            CalcFormula = lookup(FacilityFDN.Group where(Code = field(FacilityCode)));
            Caption = 'Facility Group', Comment = 'DevOps221157';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the group that the facility belongs to.', Comment = 'DevOps221157';
        }
        field(5; FacilityType; Enum RealtyObjectFacilityTypeFDN)
        {
            CalcFormula = lookup(FacilityFDN.Type where(Code = field(FacilityCode)));
            Caption = 'Facility Type', Comment = 'DevOps221157';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the type of facility that this facility belongs to.', Comment = 'DevOps221157';
        }
        field(6; FacilityName; Text[50])
        {
            CalcFormula = lookup(FacilityFDN.Name where(Code = field(FacilityCode)));
            Caption = 'Name', Comment = 'DevOps221157';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the name of the facility.', Comment = 'DevOps221157';
        }
        field(7; FacilityDescription; Text[250])
        {
            CalcFormula = lookup(FacilityFDN.Description where(Code = field(FacilityCode)));
            Caption = 'Description', Comment = 'DevOps221157';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the description of the facility.', Comment = 'DevOps221157';
        }
        field(8; FacilityMeasuringUnit; Enum RealtyObjFacilMeasureUnitFDN)
        {
            CalcFormula = lookup(FacilityFDN.MeasuringUnit where(Code = field(FacilityCode)));
            Caption = 'Measuring Unit', Comment = 'DevOps221157';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the unit of measure that the facility is shown in.', Comment = 'DevOps221157';
        }
        field(9; FacilityMeasuringValueType; Enum RealtyObjFacilityValueTypeFDN)
        {
            CalcFormula = lookup(FacilityFDN.ValueType where(Code = field(FacilityCode)));
            Caption = 'Value Type', Comment = 'DevOps221157';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the type of value that the measuring unit is shown in.', Comment = 'DevOps221157';
        }
        field(10; FacilityMeasuringValueInteger; Integer)
        {
            Caption = 'Measuring Value (Integer)', Comment = 'DevOps228220';
            ToolTip = 'Specifies the measuring value applicable for this facility and this lettable object as integer.', Comment = 'DevOps228220';
        }
        field(11; FacilityMeasuringValueDecimal; Decimal)
        {
            Caption = 'Measuring Value (Decimal)', Comment = 'DevOps228220';
            ToolTip = 'Specifies the measuring value applicable for this facility and this lettable object as decimal.', Comment = 'DevOps228220';
        }
        field(12; FacilityMeasuringValueBoolean; Boolean)
        {
            Caption = 'Measuring Value (Boolean)', Comment = 'DevOps228220';
            ToolTip = 'Specifies the measuring value applicable for this facility and this lettable object as boolean.', Comment = 'DevOps228220';
        }
        field(13; NoOfSharedAccommodations; Integer)
        {
            Caption = 'No. of Shared Accommodations', Comment = 'DevOps221157';
            ToolTip = 'Specifies the number of shared accommodations on the same address that have access to and usage right on this facility. When this number is bigger than 1, then the facility is considered to be a shared facility.', Comment = 'DevOps221157';

            trigger OnValidate()
            var
                RealtyObject: Record OGE;
            begin
                RealtyObject.SetLoadFields(Woonruimte);
                RealtyObject.Get(Rec.RealtyObjectNo);
                RealtyObject.CheckNoOfSharedAccomodationsAllowed(Rec.NoOfSharedAccommodations);
            end;
        }
        field(14; RealtyObjectAddress; Code[50])
        {
            CalcFormula = Lookup(OGE.Zoeknaam where("Nr." = field(RealtyObjectNo)));
            Caption = 'Lettable Object Address', Comment = 'DevOps228220';
            Editable = false;
            FieldClass = FlowField;
            ToolTip = 'Specifies the lettable object address where the facility is located.', Comment = 'DevOps228220';
        }
        field(15; NoOfAddresses; Integer)
        {
            Caption = 'No. of Addresses', Comment = 'DevOps229326';
            InitValue = 1;
            MinValue = 1;
            ToolTip = 'This field specifies the number of addresses that have access to and the right to use this facility. When this number is bigger than 1, then the facility is considered to be a shared facility.', Comment = 'DevOps229326';

            trigger OnValidate()
            begin
                if FacilityGroup = FacilityGroup::Special then
                    if NoOfAddresses > 1 then
                        Error(SpecialGroupAdressessErr);
            end;
        }
        field(210; DataSource; Code[20])
        {
            Caption = 'Data Source', Comment = 'DevOps232939';
            TableRelation = RealtyObjectDataSourceFDN;
            ToolTip = 'Specifies the data source the registration of the facility of the lettable object is based on.', Comment = 'DevOps232939';
        }
    }
    keys
    {
        key(PrimaryKey; RealtyObjectNo, EntryNo)
        {
            Clustered = true;
        }
        key(Key2; RealtyObjectNo, FacilityCode) { }
    }

    trigger OnInsert()
    begin
        InsertRecord();
    end;

    trigger OnModify()
    begin
        if Format(xRec) = Format(Rec) then
            xRec.Find();
        if Rec.FacilityCode <> xRec.FacilityCode then
            CheckMeasuringValue();
    end;

    trigger OnRename()
    begin
        if Rec.RealtyObjectNo <> xRec.RealtyObjectNo then
            Error(RenameNotPossibleErr);
    end;

    var
        MissingMeasuringValueErr: Label '''Measuring Value'' must have a value.', Comment = 'DevOps221157';
        PageCaptionLbl: Label '%1 - %2', Comment = '%1 = Description, %2 = Measuring Unit', Locked = true;
        RenameNotPossibleErr: Label 'It is not allowed to change the lettable object number for existing records.', Comment = 'DevOps228220';
        SpecialGroupAdressessErr: Label 'Value for field ''No. of Addresses'' must be equal to 1 when the facility belongs to group ''Special''.', Comment = 'DevOps230216';

    local procedure InsertRecord()
    begin
        InitNewEntryNo();
        CheckMeasuringValue();
    end;

    procedure InitNewEntryNo()
    var
        RealtyObjectFacilityFDN: Record RealtyObjectFacilityFDN;
    begin
        RealtyObjectFacilityFDN.SetLoadFields(EntryNo);
        RealtyObjectFacilityFDN.SetRange(RealtyObjectNo, Rec.RealtyObjectNo);
        if RealtyObjectFacilityFDN.FindLast() then
            Rec.EntryNo := RealtyObjectFacilityFDN.EntryNo + 1
        else
            Rec.EntryNo := 1;
    end;

    local procedure CheckMeasuringValue()
    begin
        Rec.CalcFields(FacilityMeasuringValueType);
        if ((Rec.FacilityMeasuringValueType = Rec.FacilityMeasuringValueType::Integer) and (Rec.FacilityMeasuringValueInteger = 0)) or
            ((Rec.FacilityMeasuringValueType = Rec.FacilityMeasuringValueType::Decimal) and (Rec.FacilityMeasuringValueDecimal = 0))
        then
            Error(MissingMeasuringValueErr);
    end;

    local procedure EmptyOldRealtyObjectFacilityValues()
    begin
        Rec.FacilityMeasuringValueBoolean := false;
        Rec.FacilityMeasuringValueDecimal := 0;
        Rec.FacilityMeasuringValueInteger := 0;
    end;

    internal procedure GetPropertyValuationSystemSharedAccomodation(): Boolean
    var
        RealtyObject: Record OGE;
    begin
        RealtyObject.SetRange("Nr.", Rec.RealtyObjectNo);
        RealtyObject.SetRange(Woonruimte, RealtyObject.Woonruimte::SharedAccommodation);
        exit(not RealtyObject.IsEmpty);
    end;

    internal procedure GetMeasuringValueText(): Text
    begin
        exit(Format(GetMeasuringValue()));
    end;

    internal procedure GetMeasuringValue(): Variant
    begin
        Rec.CalcFields(FacilityMeasuringValueType);
        case Rec.FacilityMeasuringValueType of
            RealtyObjFacilityValueTypeFDN::Boolean:
                exit(Rec.FacilityMeasuringValueBoolean);
            RealtyObjFacilityValueTypeFDN::Decimal:
                exit(Rec.FacilityMeasuringValueDecimal);
            RealtyObjFacilityValueTypeFDN::Integer:
                exit(Rec.FacilityMeasuringValueInteger);
        end;
    end;

    internal procedure SetMeasuringValue()
    begin
        Rec.CalcFields(FacilityMeasuringUnit, FacilityMeasuringValueType);
        case Rec.FacilityMeasuringValueType of
            RealtyObjFacilityValueTypeFDN::Integer:
                SetMeasuringValueInteger();
            RealtyObjFacilityValueTypeFDN::Decimal:
                SetMeasuringValueDecimal();
            RealtyObjFacilityValueTypeFDN::Boolean:
                SetMeasuringValueBoolean();
        end;
    end;

    local procedure SetMeasuringValueInteger()
    var
        SetValue: Page SetValueFDN;
    begin
        SetValue.SetPageCaption(StrSubstNo(PageCaptionLbl, Rec.FacilityName, Rec.FacilityMeasuringUnit));
        SetValue.SetValueInteger(Rec.FacilityMeasuringValueInteger);
        if SetValue.RunModal() = Action::OK then
            Rec.FacilityMeasuringValueInteger := SetValue.GetValueInteger();
    end;

    local procedure SetMeasuringValueDecimal()
    var
        SetValue: Page SetValueFDN;
    begin
        SetValue.SetPageCaption(StrSubstNo(PageCaptionLbl, Rec.FacilityName, Rec.FacilityMeasuringUnit));
        SetValue.SetValueDecimal(Rec.FacilityMeasuringValueDecimal);
        if SetValue.RunModal() = Action::OK then
            Rec.FacilityMeasuringValueDecimal := SetValue.GetValueDecimal();
    end;

    local procedure SetMeasuringValueBoolean()
    var
        SetValue: Page SetValueFDN;
    begin
        SetValue.SetPageCaption(Rec.FacilityName);
        SetValue.SetValueBoolean(Rec.FacilityMeasuringValueBoolean);
        if SetValue.RunModal() = Action::OK then
            Rec.FacilityMeasuringValueBoolean := SetValue.GetValueBoolean();
    end;

    internal procedure UpdateNoOfSharedAccommodations(RealtyObjectCode: Code[20]; NewNoOfSharedAccommodations: Integer)
    begin
        Rec.SetRange(RealtyObjectNo, RealtyObjectCode);
        Rec.ModifyAll(NoOfSharedAccommodations, NewNoOfSharedAccommodations);
    end;
}